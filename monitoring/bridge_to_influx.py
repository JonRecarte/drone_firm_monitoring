#!/usr/bin/env python3
"""Ingesta de la telemetría JSONL del bridge Webots<->Renode en InfluxDB 2.x.

El bridge de CF_renode_emulator escribe un objeto JSON por ciclo de control
cuando se lanza con --telemetry (o TELEMETRY=... en run_hover_demo.sh). Este
script lee ese fichero —completo, o siguiéndolo en vivo como `tail -F`— y lo
convierte en puntos de InfluxDB. No toca el bridge ni el monitor de Renode, que
solo admite un cliente y ya lo ocupa el bridge.

El JSONL sigue siendo el registro primario. Cada punto lleva el `t_wall`
original como timestamp, así que volver a ingerir el mismo fichero reescribe
exactamente los mismos puntos: la ingesta es idempotente y sirve para rellenar
huecos si InfluxDB estuvo caído.

Medidas que escribe:
  bridge_cycle  un punto por ciclo de control; campos crudos + derivados
  bridge_event  session_start, session_end y cualquier otro `kind`

Solo usa la biblioteca estándar, para correr con el python3 del host del bridge.

Uso:
  python3 bridge_to_influx.py --file /tmp/cf_bridge.jsonl            # reproducir
  python3 bridge_to_influx.py --file /tmp/cf_bridge.jsonl --follow   # en vivo
  python3 bridge_to_influx.py --file ejemplo.jsonl --stdout          # ver line protocol
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

MEAS_CYCLE = "bridge_cycle"
MEAS_EVENT = "bridge_event"

# Destino por defecto: el InfluxDB de docker-compose.yml con sus credenciales fijas.
DEFAULT_URL = "http://localhost:8086"
DEFAULT_ORG = "drones"
DEFAULT_BUCKET = "bridge"
DEFAULT_TOKEN = "drones-token"

# Vectores de bridge/telemetry.py -> nombres de campo legibles en InfluxDB.
# Un vector desconocido se aplana como <clave>_1, <clave>_2, ...
VECTOR_NAMES = {
    "motors": ("m1", "m2", "m3", "m4"),
    "omega": ("omega1", "omega2", "omega3", "omega4"),
    "gyro": ("gyro_x", "gyro_y", "gyro_z"),
    "accel": ("accel_x", "accel_y", "accel_z"),
}
ENVELOPE = ("t_wall", "kind")
RESERVED_TAGS = ("session", "kind", "mode")

# Marcadores que genera read_lines() además de las líneas.
IDLE = object()   # no hay datos nuevos (modo --follow): momento de vaciar el lote
RESET = object()  # el fichero se truncó o se sustituyó: empieza otra historia


def log(msg: str) -> None:
    print(f"[bridge_to_influx] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Line protocol

def _esc(s: str, chars: str) -> str:
    s = s.replace("\\", "\\\\")
    for c in chars:
        s = s.replace(c, "\\" + c)
    return s


def esc_measurement(s: str) -> str:
    return _esc(s, ", ")


def esc_key(s: str) -> str:
    """Escapa claves de tag, valores de tag y claves de campo."""
    return _esc(s, ",= ")


def fmt_field(v: Any) -> str:
    # Todos los números se escriben como float: si un campo llegara una vez como
    # entero y otra como float, InfluxDB rechazaría el punto por conflicto de tipo.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def to_line(measurement: str, tags: dict[str, Any], fields: dict[str, Any], ts_ns: int) -> str | None:
    if not fields:
        return None
    tag_s = "".join(
        f",{esc_key(str(k))}={esc_key(str(v))}"
        for k, v in sorted(tags.items())
        if v is not None and str(v) != ""
    )
    field_s = ",".join(f"{esc_key(k)}={fmt_field(v)}" for k, v in sorted(fields.items()))
    return f"{esc_measurement(measurement)}{tag_s} {field_s} {ts_ns}"


def ns(t_wall: float) -> int:
    return int(round(t_wall * 1e9))


# --------------------------------------------------------------------------
# Conversión JSONL -> puntos

@dataclass
class Stats:
    lines: int = 0
    cycles: int = 0
    events: int = 0
    sessions: int = 0
    invalid: int = 0
    non_finite: int = 0
    points_sent: int = 0
    rejected_batches: int = 0

    def summary(self) -> str:
        return (
            f"líneas={self.lines} ciclos={self.cycles} eventos={self.events} "
            f"sesiones={self.sessions} inválidas={self.invalid} "
            f"valores_no_finitos={self.non_finite} puntos_enviados={self.points_sent} "
            f"lotes_rechazados={self.rejected_batches}"
        )


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _put(out: dict[str, Any], key: str, v: Any, stats: Stats | None) -> None:
    if v is None:
        return
    if isinstance(v, (bool, str)):
        out[key] = v
    elif isinstance(v, (int, float)):
        f = float(v)
        if math.isfinite(f):
            out[key] = f
        elif stats is not None:
            # NaN/inf no caben en line protocol. Se cuentan: en el bridge son una anomalía.
            stats.non_finite += 1
    elif isinstance(v, list):
        names = VECTOR_NAMES.get(key, ())
        for i, item in enumerate(v):
            _put(out, names[i] if i < len(names) else f"{key}_{i + 1}", item, stats)
    elif isinstance(v, dict):
        for sk, sv in v.items():
            _put(out, f"{key}_{sk}", sv, stats)


def flatten(obj: dict[str, Any], stats: Stats | None = None) -> dict[str, Any]:
    """Aplana un objeto del JSONL a campos escalares, sin el sobre (t_wall, kind)."""
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k not in ENVELOPE:
            _put(out, k, v, stats)
    return out


def session_id(t_wall: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_wall))


@dataclass
class _Session:
    sid: str = "unknown"
    mode: str | None = None
    alt_target: float | None = None
    n: int = 0
    prev_t: float | None = None
    prev_sim: float | None = None
    prev_alt: float | None = None


class Converter:
    """Convierte objetos del JSONL en líneas de InfluxDB, sesión a sesión.

    TelemetryWriter abre el fichero en modo append, así que un mismo JSONL puede
    contener varias sesiones seguidas. Cada session_start abre una sesión nueva
    (tag `session`) y reinicia las derivadas que dependen del ciclo anterior.
    """

    def __init__(self, static_tags: dict[str, str] | None = None, stats: Stats | None = None):
        self.static_tags = dict(static_tags or {})
        self.stats = stats if stats is not None else Stats()
        self.session = _Session()

    def reset(self) -> None:
        self.session = _Session()

    def feed(self, obj: dict[str, Any]) -> list[str]:
        t = obj.get("t_wall")
        if not _is_num(t):
            self.stats.invalid += 1
            return []
        if "kind" in obj:
            line = self._event(obj, float(t))
        else:
            line = self._cycle(obj, float(t))
        return [line] if line else []

    def _event(self, obj: dict[str, Any], t: float) -> str | None:
        kind = str(obj["kind"])
        if kind == "session_start":
            mode = obj.get("mode")
            target = obj.get("alt_target")
            self.session = _Session(
                sid=session_id(t),
                mode=str(mode) if mode is not None else None,
                alt_target=float(target) if _is_num(target) else None,
            )
            self.stats.sessions += 1
        fields = flatten(obj, self.stats)
        payload = {k: v for k, v in obj.items() if k not in ENVELOPE}
        fields["detail"] = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        tags = {**self.static_tags, "session": self.session.sid, "kind": kind}
        self.stats.events += 1
        return to_line(MEAS_EVENT, tags, fields, ns(t))

    def _cycle(self, obj: dict[str, Any], t: float) -> str | None:
        s = self.session
        fields = flatten(obj, self.stats)
        alt = fields.get("alt") if _is_num(fields.get("alt")) else None
        sim = fields.get("sim") if _is_num(fields.get("sim")) else None
        s.n += 1

        # Derivadas. Nunca sustituyen un campo que ya traiga el bridge.
        derived: dict[str, float] = {"cycle": float(s.n)}
        if s.alt_target is not None:
            derived["alt_target"] = s.alt_target
            if alt is not None:
                derived["alt_error"] = alt - s.alt_target
        motors = [fields.get(k) for k in VECTOR_NAMES["motors"]]
        if all(_is_num(m) for m in motors):
            derived["motor_mean"] = sum(motors) / 4.0
            derived["motor_spread"] = max(motors) - min(motors)
        if s.prev_t is not None:
            dt_wall = t - s.prev_t
            if dt_wall > 0:
                derived["dt_wall"] = dt_wall
                derived["loop_hz"] = 1.0 / dt_wall
            # Con FakePlant el tiempo de Webots se queda en 0: no hay reloj simulado.
            if sim is not None and s.prev_sim is not None and (sim > 0 or s.prev_sim > 0):
                dt_sim = sim - s.prev_sim
                derived["dt_sim"] = dt_sim
                if dt_sim > 0:
                    if dt_wall > 0:
                        derived["sim_speed"] = dt_sim / dt_wall
                    if alt is not None and s.prev_alt is not None:
                        # vz física (m por segundo simulado); el `vz` del bridge usa reloj de pared.
                        derived["vz_sim"] = (alt - s.prev_alt) / dt_sim
        s.prev_t, s.prev_sim, s.prev_alt = t, sim, alt
        for k, v in derived.items():
            fields.setdefault(k, v)

        tags = {**self.static_tags, "session": s.sid}
        if s.mode:
            tags["mode"] = s.mode
        self.stats.cycles += 1
        return to_line(MEAS_CYCLE, tags, fields, ns(t))


# --------------------------------------------------------------------------
# Lectura del fichero

def read_lines(path: str, follow: bool, from_end: bool = False, poll: float = 0.5) -> Iterator[Any]:
    """Genera las líneas completas del JSONL.

    En modo `follow` genera IDLE cuando no hay datos nuevos y RESET cuando el
    fichero se trunca o se sustituye por otro. Una línea solo se entrega cuando
    termina en salto de línea: así nunca se procesa un objeto a medio escribir.
    """
    fh = None
    ino = None
    buf = b""
    announced = False
    try:
        while True:
            if fh is None:
                try:
                    fh = open(path, "rb")
                except FileNotFoundError:
                    if not follow:
                        raise
                    if not announced:
                        log(f"esperando a que aparezca {path} ...")
                        announced = True
                    yield IDLE
                    time.sleep(poll)
                    continue
                ino = os.fstat(fh.fileno()).st_ino
                if from_end:
                    fh.seek(0, os.SEEK_END)
                    from_end = False
                buf = b""

            chunk = fh.read(65536)
            if chunk:
                buf += chunk
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        yield line
                continue

            if not follow:
                tail = buf.decode("utf-8", errors="replace").strip()
                if tail:
                    yield tail
                return

            yield IDLE
            time.sleep(poll)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue  # borrado: se espera a que el bridge cree otro
            if st.st_ino != ino or st.st_size < fh.tell():
                fh.close()
                fh = None
                yield RESET
    finally:
        if fh is not None:
            fh.close()


# --------------------------------------------------------------------------
# Destinos

class InfluxFatal(Exception):
    """Error que no se arregla reintentando: credenciales, org o bucket."""


class InfluxWriter:
    def __init__(self, url: str, org: str, bucket: str, token: str,
                 max_retries: int = 5, timeout: float = 10.0, stats: Stats | None = None):
        query = urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
        self.endpoint = f"{url.rstrip('/')}/api/v2/write?{query}"
        self.headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        }
        self.max_retries = max_retries  # 0 = reintentar indefinidamente (modo --follow)
        self.timeout = timeout
        self.stats = stats if stats is not None else Stats()

    def write(self, lines: list[str]) -> None:
        body = "\n".join(lines).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(self.endpoint, data=body, headers=self.headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    return
            except urllib.error.HTTPError as e:
                detail = e.read()[:500].decode("utf-8", errors="replace")
                if e.code in (401, 403, 404):
                    raise InfluxFatal(f"InfluxDB respondió {e.code}: {detail}") from None
                if e.code == 413 and len(lines) > 1:
                    half = len(lines) // 2
                    self.write(lines[:half])
                    self.write(lines[half:])
                    return
                if e.code in (400, 422):
                    # Puntos mal formados o conflicto de tipos: reintentar no lo arregla.
                    self.stats.rejected_batches += 1
                    log(f"InfluxDB rechazó un lote ({e.code}): {detail}")
                    return
                reason = f"HTTP {e.code}: {detail}"
            except (urllib.error.URLError, OSError) as e:
                reason = str(getattr(e, "reason", e))
            if self.max_retries and attempt >= self.max_retries:
                raise ConnectionError(f"InfluxDB no disponible tras {attempt} intentos: {reason}")
            wait = min(30.0, 2.0 ** min(attempt - 1, 5))
            log(f"InfluxDB no disponible ({reason}); reintento en {wait:.0f} s")
            time.sleep(wait)


class StdoutSink:
    def write(self, lines: list[str]) -> None:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# CLI

def parse_tags(items: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise SystemExit(f"--tag espera clave=valor, recibido {item!r}")
        if key in RESERVED_TAGS:
            raise SystemExit(f"--tag {key} está reservado (lo asigna el propio ingestor)")
        tags[key] = value
    return tags


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Envía la telemetría JSONL del bridge a InfluxDB 2.x")
    p.add_argument("-f", "--file", required=True, help="JSONL escrito por el bridge (--telemetry)")
    p.add_argument("--follow", action="store_true", help="seguir el fichero en vivo, como tail -F")
    p.add_argument("--from-end", action="store_true",
                   help="con --follow, empezar al final (se pierde la sesión en curso)")
    p.add_argument("--poll", type=float, default=0.5, help="segundos entre lecturas en --follow")
    p.add_argument("--url", default=None, help=f"URL de InfluxDB (env INFLUX_URL; por defecto {DEFAULT_URL})")
    p.add_argument("--org", default=None, help=f"organización (env INFLUX_ORG; por defecto {DEFAULT_ORG})")
    p.add_argument("--bucket", default=None, help=f"bucket (env INFLUX_BUCKET; por defecto {DEFAULT_BUCKET})")
    p.add_argument("--token", default=None, help="token (env INFLUX_TOKEN; por defecto el de docker-compose.yml)")
    p.add_argument("--tag", action="append", default=[],
                   help="tag fijo clave=valor para todos los puntos (repetible), p. ej. host=lab")
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("--retries", type=int, default=5,
                   help="reintentos por lote al reproducir un fichero (en --follow, indefinidos)")
    p.add_argument("--stdout", action="store_true", help="imprimir line protocol en vez de enviarlo")
    return p.parse_args(argv)


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    # Una consola que no sepa representar un carácter no debe tumbar la ingesta.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    args = parse_args(argv)
    static_tags = parse_tags(args.tag)
    stats = Stats()
    conv = Converter(static_tags, stats)

    if args.stdout:
        sink: Any = StdoutSink()
    else:
        token = args.token or os.environ.get("INFLUX_TOKEN", DEFAULT_TOKEN)
        url = args.url or os.environ.get("INFLUX_URL", DEFAULT_URL)
        org = args.org or os.environ.get("INFLUX_ORG", DEFAULT_ORG)
        bucket = args.bucket or os.environ.get("INFLUX_BUCKET", DEFAULT_BUCKET)
        sink = InfluxWriter(url, org, bucket, token,
                            max_retries=0 if args.follow else args.retries, stats=stats)
        log(f"destino {url} org={org} bucket={bucket}")

    signal.signal(signal.SIGTERM, _on_sigterm)
    batch: list[str] = []

    def flush() -> None:
        if batch:
            sink.write(batch)
            stats.points_sent += len(batch)
            batch.clear()

    code = 0
    try:
        for item in read_lines(args.file, args.follow, args.from_end, args.poll):
            if item is IDLE:
                flush()
                continue
            if item is RESET:
                flush()
                conv.reset()
                log(f"{args.file} se truncó o se sustituyó; se lee desde el principio")
                continue
            stats.lines += 1
            try:
                obj = json.loads(item)
            except ValueError:
                stats.invalid += 1
                continue
            if not isinstance(obj, dict):
                stats.invalid += 1
                continue
            sessions_before = stats.sessions
            batch.extend(conv.feed(obj))
            if stats.sessions != sessions_before:
                s = conv.session
                log(f"sesión {s.sid} modo={s.mode} consigna={s.alt_target} m")
            if len(batch) >= args.batch_size:
                flush()
        flush()
    except KeyboardInterrupt:
        try:
            flush()
        except Exception as e:  # noqa: BLE001
            log(f"no se pudo vaciar el último lote: {e}")
    except FileNotFoundError as e:
        log(f"no existe {e.filename}")
        code = 4
    except (InfluxFatal, ConnectionError) as e:
        log(str(e))
        code = 3
    log(stats.summary())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
