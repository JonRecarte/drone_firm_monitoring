#!/usr/bin/env python3
"""Ingests the JSONL telemetry of the Webots<->Renode bridge into InfluxDB 2.x.

When launched with --telemetry, the bridge writes one JSON object per control
cycle. This script reads that file —whole, or following it live like `tail -F`—
and turns it into InfluxDB points. It never touches the bridge or the Renode
monitor, which accepts a single client and is already taken by the bridge.

The JSONL stays the primary record. Every point carries the original `t_wall`
as its timestamp, so ingesting the same file again rewrites exactly the same
points: ingestion is idempotent and can fill gaps if InfluxDB was down.

Measurements it writes:
  bridge_cycle  one point per control cycle; raw + derived fields
  bridge_event  session_start, session_end and any other `kind`

Standard library only, so it runs with the python3 of the bridge host.

Usage:
  python3 bridge_to_influx.py --file /tmp/cf_bridge.jsonl            # replay
  python3 bridge_to_influx.py --file /tmp/cf_bridge.jsonl --follow   # live
  python3 bridge_to_influx.py --file example.jsonl --stdout          # show line protocol
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

# Default target: the InfluxDB of docker-compose.yml with its fixed credentials.
DEFAULT_URL = "http://localhost:8086"
DEFAULT_ORG = "drones"
DEFAULT_BUCKET = "bridge"
DEFAULT_TOKEN = "drones-token"

# Vectors of bridge/telemetry.py -> readable field names in InfluxDB.
# An unknown vector is flattened as <key>_1, <key>_2, ...
VECTOR_NAMES = {
    "motors": ("m1", "m2", "m3", "m4"),
    "omega": ("omega1", "omega2", "omega3", "omega4"),
    "gyro": ("gyro_x", "gyro_y", "gyro_z"),
    "accel": ("accel_x", "accel_y", "accel_z"),
}
ENVELOPE = ("t_wall", "kind")
RESERVED_TAGS = ("session", "kind", "mode")

# Markers that read_lines() yields besides the lines.
IDLE = object()   # no new data (--follow mode): time to flush the batch
RESET = object()  # the file was truncated or replaced: a new history begins


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
    """Escapes tag keys, tag values and field keys."""
    return _esc(s, ",= ")


def fmt_field(v: Any) -> str:
    # Every number is written as a float: if a field arrived once as an integer
    # and once as a float, InfluxDB would reject the point for a type conflict.
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
# JSONL -> points

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
            f"lines={self.lines} cycles={self.cycles} events={self.events} "
            f"sessions={self.sessions} invalid={self.invalid} "
            f"non_finite_values={self.non_finite} points_sent={self.points_sent} "
            f"rejected_batches={self.rejected_batches}"
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
            # NaN/inf do not fit in line protocol. They are counted: in the bridge they are an anomaly.
            stats.non_finite += 1
    elif isinstance(v, list):
        names = VECTOR_NAMES.get(key, ())
        for i, item in enumerate(v):
            _put(out, names[i] if i < len(names) else f"{key}_{i + 1}", item, stats)
    elif isinstance(v, dict):
        for sk, sv in v.items():
            _put(out, f"{key}_{sk}", sv, stats)


def flatten(obj: dict[str, Any], stats: Stats | None = None) -> dict[str, Any]:
    """Flattens a JSONL object into scalar fields, without the envelope (t_wall, kind)."""
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
    """Turns JSONL objects into InfluxDB lines, session by session.

    TelemetryWriter opens the file in append mode, so one JSONL can hold
    several sessions in a row. Each session_start opens a new session
    (tag `session`) and resets the derived fields that depend on the previous cycle.
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

        # Derived fields. They never replace a field the bridge already sends.
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
            # With FakePlant the Webots time stays at 0: there is no simulated clock.
            if sim is not None and s.prev_sim is not None and (sim > 0 or s.prev_sim > 0):
                dt_sim = sim - s.prev_sim
                derived["dt_sim"] = dt_sim
                if dt_sim > 0:
                    if dt_wall > 0:
                        derived["sim_speed"] = dt_sim / dt_wall
                    if alt is not None and s.prev_alt is not None:
                        # Physical vz (m per simulated second), computed here from the logged samples.
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
# Reading the file

def read_lines(path: str, follow: bool, from_end: bool = False, poll: float = 0.5) -> Iterator[Any]:
    """Yields the complete lines of the JSONL.

    In `follow` mode it yields IDLE when there is no new data and RESET when the
    file is truncated or replaced by another one. A line is only delivered once
    it ends in a newline, so a half-written object is never processed.
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
                        log(f"waiting for {path} to appear ...")
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
                continue  # deleted: wait for the bridge to create another one
            if st.st_ino != ino or st.st_size < fh.tell():
                fh.close()
                fh = None
                yield RESET
    finally:
        if fh is not None:
            fh.close()


# --------------------------------------------------------------------------
# Sinks

class InfluxFatal(Exception):
    """An error that retrying will not fix: credentials, org or bucket."""


class InfluxWriter:
    def __init__(self, url: str, org: str, bucket: str, token: str,
                 max_retries: int = 5, timeout: float = 10.0, stats: Stats | None = None):
        query = urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
        self.endpoint = f"{url.rstrip('/')}/api/v2/write?{query}"
        self.headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        }
        self.max_retries = max_retries  # 0 = retry forever (--follow mode)
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
                    raise InfluxFatal(f"InfluxDB answered {e.code}: {detail}") from None
                if e.code == 413 and len(lines) > 1:
                    half = len(lines) // 2
                    self.write(lines[:half])
                    self.write(lines[half:])
                    return
                if e.code in (400, 422):
                    # Malformed points or a type conflict: retrying will not fix it.
                    self.stats.rejected_batches += 1
                    log(f"InfluxDB rejected a batch ({e.code}): {detail}")
                    return
                reason = f"HTTP {e.code}: {detail}"
            except (urllib.error.URLError, OSError) as e:
                reason = str(getattr(e, "reason", e))
            if self.max_retries and attempt >= self.max_retries:
                raise ConnectionError(f"InfluxDB unavailable after {attempt} attempts: {reason}")
            wait = min(30.0, 2.0 ** min(attempt - 1, 5))
            log(f"InfluxDB unavailable ({reason}); retrying in {wait:.0f} s")
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
            raise SystemExit(f"--tag expects key=value, got {item!r}")
        if key in RESERVED_TAGS:
            raise SystemExit(f"--tag {key} is reserved (the ingester sets it)")
        tags[key] = value
    return tags


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sends the bridge's JSONL telemetry to InfluxDB 2.x")
    p.add_argument("-f", "--file", required=True, help="JSONL written by the bridge (--telemetry)")
    p.add_argument("--follow", action="store_true", help="follow the file live, like tail -F")
    p.add_argument("--from-end", action="store_true",
                   help="with --follow, start at the end (the current session is lost)")
    p.add_argument("--poll", type=float, default=0.5, help="seconds between reads in --follow")
    p.add_argument("--url", default=None, help=f"InfluxDB URL (env INFLUX_URL; default {DEFAULT_URL})")
    p.add_argument("--org", default=None, help=f"organization (env INFLUX_ORG; default {DEFAULT_ORG})")
    p.add_argument("--bucket", default=None, help=f"bucket (env INFLUX_BUCKET; default {DEFAULT_BUCKET})")
    p.add_argument("--token", default=None, help="token (env INFLUX_TOKEN; default the one in docker-compose.yml)")
    p.add_argument("--tag", action="append", default=[],
                   help="fixed key=value tag for every point (repeatable), e.g. host=lab")
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("--retries", type=int, default=5,
                   help="retries per batch when replaying a file (unlimited with --follow)")
    p.add_argument("--stdout", action="store_true", help="print line protocol instead of sending it")
    return p.parse_args(argv)


def _on_sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    # A console that cannot print a character must not bring ingestion down.
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
        log(f"target {url} org={org} bucket={bucket}")

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
                log(f"{args.file} was truncated or replaced; reading from the start")
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
                log(f"session {s.sid} mode={s.mode} target={s.alt_target} m")
            if len(batch) >= args.batch_size:
                flush()
        flush()
    except KeyboardInterrupt:
        try:
            flush()
        except Exception as e:  # noqa: BLE001
            log(f"could not flush the last batch: {e}")
    except FileNotFoundError as e:
        log(f"{e.filename} does not exist")
        code = 4
    except (InfluxFatal, ConnectionError) as e:
        log(str(e))
        code = 3
    log(stats.summary())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
