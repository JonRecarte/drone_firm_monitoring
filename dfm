#!/usr/bin/env python3
"""dfm — drone_firm_monitoring: real, emulated firmware for the drones in a Webots world.

The user opens their world in Webots and presses ▶. Every drone with controller "<extern>"
waits for a controller, and Webots creates one socket per drone in
/tmp/webots/<user>/<port>/ipc/<drone>/extern. dfm finds the drones through those sockets
and, for each one, starts:
  - a Renode container running the real Crazyflie firmware,
  - a bridge that connects that Renode to its drone in Webots,
  - an ingester that sends its telemetry to InfluxDB/Grafana (if monitoring is enabled).
dfm never opens or closes Webots.

Commands:
  dfm setup      once: checks requirements, downloads the images and puts dfm on the PATH
  dfm check      tests the installation without Webots (firmware, motors and monitoring)
  dfm up         finds the drones and starts everything; Ctrl+C stops it
  dfm status     which Webots and drones it sees, and what is still running
  dfm down       stops anything left over from a previous run
  dfm uninstall  removes what setup installed (never touches Webots, Docker or your worlds)

Standard library only (Python ≥ 3.11).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("dfm needs Python 3.11 or newer (it uses tomllib).")
import tomllib  # noqa: E402

REPO = Path(__file__).resolve().parent
ENGINE = REPO / "engine"        # bridge and Renode monitor commands (see ORIGIN.md)
MONITORING = REPO / "monitoring"
RUNS = REPO / "runs"
LABEL = "dfm"
# Version of this repo. The image with the same version is published on Docker Hub by
# .github/workflows/image.yml when the tag v<VERSION> is pushed.
VERSION = "0.1.0"
IMAGE = f"jonrecartenaitec/drone_firm_monitoring:{VERSION}"

# Defaults; config.toml (or config.example.toml) overrides them.
DEFAULTS: dict = {
    "renode": {"image": IMAGE, "base_port": 10000},
    "bridge": {
        "mode": "closed-loop",
        "alt_target": 0.5,
        "thrust": 30500,
        "hover_thrust": 31000,
        "imu_hz": 80,
        "loop_hz": 12,
        "duration_s": 300,
        "gate_timeout_s": 600,
        # Altitude PD+I, in simulated time (see engine/bridge/webots_renode_bridge.py).
        "alt_kp": 0.40, "alt_kd": 0.341, "alt_ki": 0.129, "alt_i_limit": 0.31, "alt_vz_limit": 8.06,
    },
    "monitoring": {"enabled": True},
}


def log(msg: str) -> None:
    print(f"[dfm] {msg}", file=sys.stderr, flush=True)


def fatal(msg: str) -> None:
    log(msg)
    sys.exit(1)


# --------------------------------------------------------------------------
# Configuration

def load_config() -> tuple[dict, Path]:
    path = REPO / "config.toml"
    if not path.exists():
        path = REPO / "config.example.toml"
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
    return cfg, path


# --------------------------------------------------------------------------
# Webots: running process and drones waiting for a controller

def webots_home() -> Path | None:
    """WEBOTS_HOME of the Webots the user has open (or the one in the environment)."""
    if os.environ.get("WEBOTS_HOME"):
        return Path(os.environ["WEBOTS_HOME"])
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            if (pid_dir / "comm").read_text().strip() != "webots-bin":
                continue
            # exe is only readable when user and group match; cmdline always is.
            try:
                exe = Path(os.readlink(pid_dir / "exe"))
            except OSError:
                exe = Path((pid_dir / "cmdline").read_bytes().split(b"\0")[0].decode())
        except OSError:
            continue
        if exe.name == "webots-bin" and exe.is_absolute():
            return exe.parent.parent
    return None


def webots_version(home: Path) -> str:
    try:
        out = subprocess.run([str(home / "webots"), "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout + out.stderr).strip().splitlines()[-1]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "unknown"


WEBOTS_VERSION = "R2025a"
WEBOTS_CANDIDATES = ("/usr/local/webots", "~/.local/webots-R2025a", "/snap/webots/current/usr/share/webots", "/opt/webots")


def find_webots() -> Path | None:
    """Webots installation: the one running, WEBOTS_HOME, or the usual locations."""
    home = webots_home()
    if home:
        return home
    for candidate in WEBOTS_CANDIDATES:
        path = Path(candidate).expanduser()
        if (path / "webots").exists():
            return path
    return None


def missing_libs(home: Path) -> list[str]:
    """System libraries Webots is missing (the tarball often lacks libsndio)."""
    env = dict(os.environ, LD_LIBRARY_PATH=str(home / "lib" / "webots"))
    try:
        out = subprocess.run(["ldd", str(home / "bin" / "webots-bin")], capture_output=True, text=True, env=env).stdout
    except OSError:
        return []
    return [line.split()[0] for line in out.splitlines() if "not found" in line]


def discover() -> dict[int, list[str]]:
    """{Webots port: [drones with an <extern> controller waiting]}."""
    user = getpass.getuser()
    bases = [Path(p) for p in (os.environ.get("WEBOTS_TMPDIR"), "/tmp", str(Path.home() / "snap/webots/common/tmp")) if p]
    found: dict[int, list[str]] = {}
    for base in bases:
        root = base / "webots" / user
        if not root.is_dir():
            continue
        for port_dir in root.iterdir():
            if not port_dir.name.isdigit():
                continue
            robots = sorted(p.parent.name for p in port_dir.glob("ipc/*/extern") if p.is_socket())
            if robots:
                found[int(port_dir.name)] = robots
    return found


# --------------------------------------------------------------------------
# Docker

def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {(res.stderr or res.stdout).strip()}")
    return res


def container_name(drone: str) -> str:
    return "dfm-renode-" + re.sub(r"[^a-zA-Z0-9_.-]", "_", drone)


def container_running(name: str) -> bool:
    res = docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    return res.returncode == 0 and res.stdout.strip() == "true"


# --------------------------------------------------------------------------
# Renode: one container from the image per drone

def image_labels(image: str) -> dict:
    """Image labels (firmware and Renode it contains); stops if the image is missing."""
    res = docker("image", "inspect", "-f", "{{json .Config.Labels}}", image, check=False)
    if res.returncode != 0:
        fatal(f"Image {image} not found. Run dfm setup, or build it with: docker build -f image/Dockerfile -t {image} .")
    return json.loads(res.stdout) or {}


def start_renode(drone: str, port: int, image: str, run_id: str) -> str:
    """The image entrypoint (image/entrypoint.py) prepares firmware, profile and monitor.py."""
    name = container_name(drone)
    docker("rm", "-f", name, check=False)
    docker(
        "run", "-d", "--name", name,
        "--label", f"{LABEL}=1", "--label", f"{LABEL}.run={run_id}", "--label", f"{LABEL}.drone={drone}",
        "-p", f"127.0.0.1:{port}:9999",
        # Monitor commands come from the repo, so they always match its bridge.
        "-v", f"{ENGINE / 'scripts' / 'monitor_cf.py'}:/dfm/monitor_cf.py:ro",
        image,
    )
    return name


def wait_machine_started(names: list[str], timeout: float = 120.0) -> None:
    """Waits for "Machine started" in the logs. The monitor port is not probed: Renode
    accepts a single client and shuts down when that client disconnects."""
    pending, end = set(names), time.time() + timeout
    while pending and time.time() < end:
        for name in list(pending):
            res = docker("logs", name, check=False)
            if "Machine started" in res.stdout + res.stderr:
                pending.discard(name)
            elif not container_running(name):
                raise RuntimeError(f"container {name} stopped while starting")
        time.sleep(1)
    if pending:
        raise RuntimeError(f"Renode did not start in time: {', '.join(sorted(pending))}")


# --------------------------------------------------------------------------
# Per-drone processes: bridge and ingester

@dataclass
class Drone:
    name: str
    renode_port: int
    container: str = ""
    jsonl: Path = Path()
    bridge: subprocess.Popen | None = None
    bridge_log: Path = Path()
    ingest: subprocess.Popen | None = None
    extra: dict = field(default_factory=dict)


def bridge_args(cfg: dict, d: Drone) -> list[str]:
    b = cfg["bridge"]
    return [
        sys.executable, "-u", "-m", "bridge.webots_renode_bridge",
        "--mode", str(b["mode"]), "--wait-preflight", "--gate-timeout", str(b["gate_timeout_s"]),
        "--imu-hz", str(b["imu_hz"]), "--loop-hz", str(b["loop_hz"]), "--seconds", str(b["duration_s"]),
        "--status-every", "2", "--port", str(d.renode_port), "--gyro-sign", "1,1,1", "--gyro-lpf-tau", "0.02",
        "--telemetry", str(d.jsonl),
        "--thrust", str(b["thrust"]), "--hover-thrust", str(b["hover_thrust"]),
        "--roll", "0", "--pitch", "0", "--yaw-rate", "0", "--alt-target", str(b["alt_target"]),
        "--alt-kp", str(b["alt_kp"]), "--alt-kd", str(b["alt_kd"]), "--alt-ki", str(b["alt_ki"]),
        "--alt-i-limit", str(b["alt_i_limit"]), "--alt-vz-limit", str(b["alt_vz_limit"]), "--alt-hold",
    ]


def start_bridge(cfg: dict, d: Drone, home: Path, webots_port: int, run_dir: Path) -> None:
    env = os.environ.copy()
    env["WEBOTS_HOME"] = str(home)
    env["WEBOTS_CONTROLLER_URL"] = f"ipc://{webots_port}/{d.name}"
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(home / "lib/controller/python"), env.get("PYTHONPATH")) if p)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        p for p in (str(home / "lib/controller"), str(home / "lib/webots"), env.get("LD_LIBRARY_PATH")) if p
    )
    d.bridge_log = run_dir / f"{d.name}.bridge.log"
    d.bridge = subprocess.Popen(
        bridge_args(cfg, d), cwd=ENGINE, env=env,
        stdout=open(d.bridge_log, "w"), stderr=subprocess.STDOUT, start_new_session=True,
    )


def start_ingest(cfg: dict, d: Drone, run_id: str, run_dir: Path) -> None:
    script = MONITORING / "bridge_to_influx.py"
    d.ingest = subprocess.Popen(
        [sys.executable, str(script), "--file", str(d.jsonl), "--follow",
         "--tag", f"drone={d.name}", "--tag", f"run={run_id}"],
        stdout=open(run_dir / f"{d.name}.ingest.log", "w"), stderr=subprocess.STDOUT, start_new_session=True,
    )


# --------------------------------------------------------------------------
# State of each drone, read from its bridge log

FLIGHT_RE = re.compile(r" n=(\d+) sim=([\d.]+) alt=(-?[\d.]+) .* m=\(([\d.]+),([\d.]+),([\d.]+),([\d.]+)\)")
GATE_RE = re.compile(r"calib=(\d) preflight=(\d)")


def bridge_state(d: Drone) -> tuple[str, bool]:
    """(text for the table, is it an error?)."""
    try:
        text = d.bridge_log.read_text(errors="replace")
    except OSError:
        return "no log", False
    if "Using FakePlant" in text:
        return "did NOT connect to Webots (would use FakePlant)", True
    if "TIMEOUT waiting for gates" in text:
        return "firmware did not pass calibration/preflight in time", True
    if "Traceback" in text:
        return "bridge error (see its log)", True
    if "Webots quit." in text:
        return "Webots was closed", True
    if "Done after" in text:
        return "finished", False
    flights = FLIGHT_RE.findall(text)
    if flights:
        n, sim, alt, *m = flights[-1]
        mean = sum(float(x) for x in m) / 4
        state = "arming" if mean < 0.05 else "flying"
        return f"{state}  alt={float(alt):.2f} m  motors={mean:.2f}  sim={float(sim):.1f} s", False
    gates = GATE_RE.findall(text)
    if gates:
        c, p = gates[-1]
        if p == "1":
            return "preflight OK", False
        return ("calibrated, waiting for preflight" if c == "1" else "booting firmware (calibrating)"), False
    if "Renode connected" in text:
        return "connected to Renode", False
    return "starting", False


_table_rows = 0  # terminal rows taken by the last table, to redraw it in place


def print_table(drones: list[Drone], webots_port: int, grafana: bool, started: float) -> None:
    global _table_rows
    count = f"{len(drones)} drone" + ("" if len(drones) == 1 else "s")
    lines = [
        f"dfm · Webots (port {webots_port}) · {count} · {time.time() - started:5.0f} s   (Ctrl+C to stop)",
        "",
        f"  {'drone':<12} {'renode':<10} bridge",
    ]
    for d in drones:
        renode = "running" if container_running(d.container) else "STOPPED"
        state, _ = bridge_state(d)
        lines.append(f"  {d.name:<12} {renode:<10} {state}")
    idle = time.time() - max((d.bridge_log.stat().st_mtime for d in drones if d.bridge_log.exists()), default=time.time())
    if idle > 60:
        lines += ["", f"  ⚠ The simulation has not advanced for {idle:.0f} s: is Webots paused?"]
    if grafana:
        lines += ["", "  Grafana: http://localhost:3000"]
    if sys.stdout.isatty():
        # Move back over the previous table and clear it, so earlier output stays visible above.
        back = f"\x1b[{_table_rows}F\x1b[J" if _table_rows else ""
        print(back + "\n".join(lines), flush=True)
        cols = shutil.get_terminal_size().columns
        _table_rows = sum(max(1, -(-len(line) // cols)) for line in lines)
    else:
        print("\n".join(lines) + "\n", flush=True)


# --------------------------------------------------------------------------
# up, status, down

def cmd_up(args: argparse.Namespace) -> int:
    cfg, cfg_path = load_config()
    if args.duration is not None:
        cfg["bridge"]["duration_s"] = args.duration
    if args.no_monitoring:
        cfg["monitoring"]["enabled"] = False
    labels = image_labels(cfg["renode"]["image"])
    if shutil.which("docker") is None or docker("ps", check=False).returncode != 0:
        fatal("Cannot use Docker without sudo. Is it installed and is your user in the docker group?")

    home = webots_home()
    if home is None:
        log("Webots is not running. Open your world in Webots and press ▶; waiting…")
    found: dict[int, list[str]] = {}
    while not found:
        found = discover()
        if args.port:
            found = {p: r for p, r in found.items() if p == args.port}
        if not found:
            time.sleep(2)
    if len(found) > 1:
        fatal(f"Several Webots instances have drones waiting ({sorted(found)}); pick one with --port")
    webots_port, names = next(iter(found.items()))
    home = home or webots_home()
    if home is None:
        fatal("Drones are waiting but the Webots process was not found; set WEBOTS_HOME")
    version = webots_version(home)
    log(f"{version} at {home}, port {webots_port}: {len(names)} drones waiting ({', '.join(names)})")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    base = int(cfg["renode"]["base_port"])
    drones = [Drone(name=n, renode_port=base + i + 1, jsonl=run_dir / f"{n}.jsonl") for i, n in enumerate(names)]
    (run_dir / "run.json").write_text(json.dumps({
        "run": run_id, "config": cfg, "config_file": str(cfg_path),
        "webots": {"home": str(home), "version": version, "port": webots_port},
        "drones": {d.name: {"renode_port": d.renode_port} for d in drones},
        "firmware": {"ref": labels.get("dfm.firmware.ref"), "sha256": labels.get("dfm.firmware.sha256")},
        "renode_ref": labels.get("dfm.renode.ref"),
        "repo_commit": subprocess.run(["git", "-C", str(REPO), "describe", "--always", "--dirty"],
                                       capture_output=True, text=True).stdout.strip(),
        "image": cfg["renode"]["image"],
    }, indent=2, ensure_ascii=False))

    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(requested=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(requested=True))
    reason = "finished"
    started = time.time()
    try:
        log(f"Starting {len(drones)} Renode containers…")
        for d in drones:
            d.container = start_renode(d.name, d.renode_port, cfg["renode"]["image"], run_id)
        wait_machine_started([d.container for d in drones])
        if cfg["monitoring"]["enabled"]:
            compose = MONITORING / "docker-compose.yml"
            docker("compose", "-f", str(compose), "up", "-d")
            for d in drones:
                start_ingest(cfg, d, run_id, run_dir)
        for d in drones:
            start_bridge(cfg, d, home, webots_port, run_dir)
        (run_dir / "pids.json").write_text(json.dumps(
            {d.name: {"bridge": d.bridge.pid, "ingest": d.ingest.pid if d.ingest else None} for d in drones}))
        while not stop["requested"]:
            print_table(drones, webots_port, cfg["monitoring"]["enabled"], started)
            errors = [(d, s) for d in drones for s, e in [bridge_state(d)] if e]
            if errors:
                reason = "; ".join(f"{d.name}: {s}" for d, s in errors)
                break
            # A bridge that finishes its flight exits with 0 and prints "Done after"; when it
            # closes the monitor its Renode stops. That is a normal end, not a failure.
            done = [d for d in drones if d.bridge.poll() is not None]
            failed = [d for d in done if d.bridge.returncode != 0 or bridge_state(d)[0] != "finished"]
            lost = [d for d in drones if d.bridge.poll() is None and not container_running(d.container)]
            if failed or lost:
                reason = ", ".join(d.name for d in failed + lost) + " stopped early"
                break
            if done:
                # Webots steps all drones together and, when one loses its controller, it
                # stops and waits for it ("'cf2' extern controller: Waiting…", seen on 2 Nov).
                # As soon as one bridge finishes its flight, stop the rest: they flew as long.
                break
            time.sleep(2)
        else:
            reason = "stopped by the user"
        print_table(drones, webots_port, cfg["monitoring"]["enabled"], started)
    except RuntimeError as exc:
        reason = str(exc)
    finally:
        shutdown(drones)
    log(f"End: {reason}. Results in {run_dir}")
    return 0 if reason in ("finished", "stopped by the user") else 1


def shutdown(drones: list[Drone]) -> None:
    """Stops bridges (SIGINT clears the setpoint), ingesters and containers. Webots is untouched."""
    for d in drones:
        if d.bridge and d.bridge.poll() is None:
            d.bridge.send_signal(signal.SIGINT)
    # A bridge blocked in a Webots step may not handle SIGINT: SIGKILL after 10 s.
    deadline = time.time() + 10
    for d in drones:
        if d.bridge:
            try:
                d.bridge.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                d.bridge.kill()
    time.sleep(2)  # let the ingesters read the last lines
    for d in drones:
        if d.ingest and d.ingest.poll() is None:
            d.ingest.send_signal(signal.SIGINT)
            try:
                d.ingest.wait(timeout=10)
            except subprocess.TimeoutExpired:
                d.ingest.kill()
        if d.container:
            docker("rm", "-f", d.container, check=False)


def cmd_status(args: argparse.Namespace) -> int:
    home = webots_home()
    print(f"Webots running: {'yes, at ' + str(home) if home else 'no'}")
    found = discover()
    if not found:
        print("Drones waiting for a controller: none (is the world open and ▶ pressed?)")
    for port, robots in sorted(found.items()):
        print(f"Drones waiting for a controller on port {port}: {', '.join(robots)}")
    res = docker("ps", "-a", "--filter", f"label={LABEL}=1", "--format", "{{.Names}}\t{{.Status}}", check=False)
    print("dfm containers:" + ("\n  " + res.stdout.strip().replace("\n", "\n  ") if res.stdout.strip() else " none"))
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    for pids_file in sorted(RUNS.glob("*/pids.json")):
        for procs in json.loads(pids_file.read_text()).values():
            for pid in procs.values():
                if pid:
                    try:
                        os.kill(pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
    res = docker("ps", "-aq", "--filter", f"label={LABEL}=1", check=False)
    ids = res.stdout.split()
    if ids:
        docker("rm", "-f", *ids, check=False)
    log(f"Stopped {len(ids)} dfm containers. Webots is untouched.")
    return 0


# --------------------------------------------------------------------------
# setup, check, uninstall

BIN = Path.home() / ".local" / "bin"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True   # if not required, it is only a warning


def requirements(cfg: dict) -> list[Check]:
    """What the user provides (dfm does not install it): Linux, Docker, Python and Webots."""
    checks = [
        Check("Linux x86_64", sys.platform.startswith("linux") and platform.machine() == "x86_64",
              f"{platform.system()} {platform.machine()}", "dfm only runs on Linux x86_64"),
        Check("Python ≥ 3.11", True, platform.python_version()),
    ]
    if shutil.which("docker") is None:
        checks.append(Check("Docker", False, "not installed", "Install Docker Engine: https://docs.docker.com/engine/install/"))
    else:
        res = docker("ps", check=False)
        if res.returncode == 0:
            version = docker("version", "--format", "{{.Server.Version}}", check=False).stdout.strip()
            checks.append(Check("Docker without sudo", True, version))
        elif "permission denied" in (res.stderr or "").lower():
            checks.append(Check("Docker without sudo", False, "no permission to use Docker",
                                "sudo usermod -aG docker $USER, then log out and back in (close the terminal or SSH session)"))
        else:
            checks.append(Check("Docker without sudo", False, (res.stderr or "").strip()[:80],
                                "Is the service running? sudo systemctl start docker"))
        compose = docker("compose", "version", "--short", check=False)
        checks.append(Check("Docker Compose", compose.returncode == 0, compose.stdout.strip() or "not installed",
                            "Install the docker-compose-plugin package", required=bool(cfg["monitoring"]["enabled"])))
    home = find_webots()
    if home is None:
        checks.append(Check(f"Webots {WEBOTS_VERSION}", False, "not found",
                            f"Install Webots {WEBOTS_VERSION} (https://cyberbotics.com) or set WEBOTS_HOME"))
    else:
        version = webots_version(home)
        checks.append(Check(f"Webots {WEBOTS_VERSION}", WEBOTS_VERSION in version, f"{version} at {home}",
                            f"dfm needs Webots {WEBOTS_VERSION}"))
        lib = home / "lib" / "controller" / "libController.so"
        checks.append(Check("Webots controller library", lib.exists(), str(lib), "The Webots installation is incomplete"))
        missing = missing_libs(home)
        checks.append(Check("Libraries used by Webots", not missing, "all present" if not missing else "missing " + ", ".join(missing),
                            "Install the missing packages (the Webots tarball often needs libsndio7.0)", required=False))
    free = shutil.disk_usage(REPO).free / 1e9
    checks.append(Check("Disk space", free > 3, f"{free:.0f} GB free", "About 3 GB are needed", required=False))
    ncpu = os.cpu_count() or 1
    checks.append(Check("Capacity", True, f"{ncpu} cores: ~1 core and ~250 MB per drone; about {max(1, ncpu - 2)} drones fit next to Webots"))
    return checks


def print_checks(checks: list[Check]) -> bool:
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.required else "!")
        print(f"  {mark} {c.name:<28} {c.detail}")
        if not c.ok and c.fix:
            print(f"      → {c.fix}")
    return all(c.ok or not c.required for c in checks)


def compose_images() -> list[str]:
    text = (MONITORING / "docker-compose.yml").read_text()
    return re.findall(r"^\s*image:\s*(\S+)", text, re.M)


def link_command() -> bool:
    """Makes `dfm` available from any folder (link in ~/.local/bin). True if `dfm` now works."""
    BIN.mkdir(parents=True, exist_ok=True)
    link, target = BIN / "dfm", Path(__file__).resolve()
    if link.is_symlink() or link.exists():
        if link.resolve() != target:
            print(f"  ! {link} already exists and is not this dfm: leaving it alone")
            return False
        link.unlink()
    link.symlink_to(target)
    print(f"  ✓ {link} → {target}")
    if str(BIN) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"  ! {BIN} is not on your PATH. Add this line to ~/.bashrc and open a new terminal:")
        print('      export PATH="$HOME/.local/bin:$PATH"')
        print("    Until then, use ./dfm from this folder.")
        return False
    return True


def cmd_setup(args: argparse.Namespace) -> int:
    cfg, _ = load_config()
    print("Requirements (you provide them; dfm does not install them):")
    if not print_checks(requirements(cfg)):
        print("\nFix the items marked ✗ and run ./dfm setup again.")
        return 1
    image = cfg["renode"]["image"]
    print("\nImages:")
    if docker("image", "inspect", image, check=False).returncode == 0:
        print(f"  ✓ {image} (already present)")
    else:
        print(f"  … downloading {image}", flush=True)
        if docker("pull", image, check=False, capture=False).returncode != 0:
            print(f"  ✗ Could not download {image}. If you are developing the image, build it with:")
            print(f"      docker build -f image/Dockerfile -t {image} .")
            return 1
        print(f"  ✓ {image}")
    if cfg["monitoring"]["enabled"]:
        compose = str(MONITORING / "docker-compose.yml")
        print("  … InfluxDB and Grafana", flush=True)
        if docker("compose", "-f", compose, "pull", "-q", check=False).returncode != 0 or \
           docker("compose", "-f", compose, "up", "-d", check=False).returncode != 0:
            print("  ✗ Could not start monitoring (InfluxDB and Grafana)")
            return 1
        print("  ✓ InfluxDB and Grafana running · Grafana: http://localhost:3000 (admin / admin)")
    RUNS.mkdir(exist_ok=True)
    print("\nCommand:")
    command = "dfm" if link_command() else "./dfm"
    print(f"\nDone. Next: {command} check  (tests everything without Webots; takes a few minutes)")
    print(f"To fly: open a world in Webots (e.g. examples/worlds/one_drone.wbt), press ▶ and run {command} up")
    return 0


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            return res.status == 200
    except (OSError, urllib.error.URLError):
        return False


def cmd_check(args: argparse.Namespace) -> int:
    """Tests the installation without Webots: the firmware boots in Renode, calibrates,
    passes preflight and drives the motors with a setpoint, and monitoring answers."""
    cfg, _ = load_config()
    ok = True
    print("1. Requirements:")
    ok &= print_checks(requirements(cfg))
    image = cfg["renode"]["image"]
    print("\n2. Image:")
    res = docker("image", "inspect", "-f", "{{json .Config.Labels}}", image, check=False)
    if res.returncode != 0:
        print(f"  ✗ {image} not found. Run dfm setup.")
        return 1
    labels = json.loads(res.stdout) or {}
    print(f"  ✓ {image} · firmware {labels.get('dfm.firmware.ref')} ({str(labels.get('dfm.firmware.sha256'))[:12]}…)")

    print("\n3. Firmware in Renode (no Webots):")
    port, name = int(cfg["renode"]["base_port"]), "dfm-check"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--name", name, "--label", f"{LABEL}=1", "-p", f"127.0.0.1:{port}:9999",
           "-v", f"{ENGINE / 'scripts' / 'monitor_cf.py'}:/dfm/monitor_cf.py:ro", image)
    try:
        t0 = time.time()
        wait_machine_started([name])
        print(f"  ✓ Renode starts ({time.time() - t0:.0f} s)")
        if args.quick:
            print("  · --quick: not waiting for calibration or motors")
        else:
            ok &= check_firmware(port, float(cfg["bridge"]["gate_timeout_s"]))
    except RuntimeError as exc:
        print(f"  ✗ {exc}")
        ok = False
    finally:
        docker("rm", "-f", name, check=False)

    if cfg["monitoring"]["enabled"]:
        print("\n4. Monitoring:")
        for service, url in (("InfluxDB", "http://127.0.0.1:8086/health"), ("Grafana", "http://127.0.0.1:3000/api/health")):
            up = http_ok(url)
            ok &= up
            print(f"  {'✓' if up else '✗'} {service}" + ("" if up else " is not answering: run dfm setup"))
    print("\n" + ("All OK." if ok else "Something is wrong: check the lines marked ✗."))
    return 0 if ok else 1


def check_firmware(port: int, gate_timeout: float) -> bool:
    sys.path.insert(0, str(ENGINE / "scripts"))
    from renode_monitor import RenodeClient

    client = RenodeClient(port)
    try:
        client.query("imu_stream_start 80 0 0 0 0 0 1000 50 101325 25")
        t0, last, calib, pre = time.time(), 0.0, False, False
        while time.time() - t0 < gate_timeout and not pre:
            calib = "calibrated=1" in client.query("calib_status")
            pre = "preflight_ok=1" in client.query("preflight_status")
            if time.time() - last > 20:
                print(f"  … booting the firmware ({time.time() - t0:.0f} s; calibrated={'yes' if calib else 'no'})", flush=True)
                last = time.time()
            time.sleep(5)  # every monitor query slows the emulation down
        if not pre:
            print(f"  ✗ The firmware did not pass calibration and preflight within {gate_timeout:.0f} s")
            return False
        print(f"  ✓ The firmware calibrates and passes preflight ({time.time() - t0:.0f} s)")
        t1 = time.time()
        while time.time() - t1 < 180:
            client.query("setpoint_rpyt 0 0 0 37000")
            m = re.search(r"m1=(\d+)", client.query("motor_stats"))
            if m and int(m.group(1)) > 0:
                print(f"  ✓ The firmware arms and drives the motors from a setpoint ({time.time() - t1:.0f} s)")
                return True
            time.sleep(0.2)
        print("  ✗ The firmware did not drive the motors within 180 s")
        return False
    finally:
        client.close()


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Removes what dfm setup installed. Webots, Docker and the user's worlds are untouched."""
    cfg, _ = load_config()
    delete = args.delete_data
    if not delete and not args.dry_run and sys.stdin.isatty():
        delete = input("Also delete the data (flights in InfluxDB/Grafana and the runs/ folder)? [y/N] ").strip().lower() in ("y", "yes")
    # The configured image and the published one, in case config.toml points elsewhere.
    images = list(dict.fromkeys([cfg["renode"]["image"], IMAGE, *compose_images()]))
    link = BIN / "dfm"
    ours = link.is_symlink() and link.resolve() == Path(__file__).resolve()
    if args.dry_run:
        print("Dry run: this is what dfm uninstall would do")
        print("  · stop the dfm containers and monitoring" + (" and DELETE their data" if delete else " (data kept)"))
        print("  · remove the images: " + ", ".join(images))
        if delete:
            print(f"  · delete {RUNS}")
        if ours:
            print(f"  · remove {link}")
        return 0
    cmd_down(args)
    docker("compose", "-f", str(MONITORING / "docker-compose.yml"), "down", *(["-v"] if delete else []), check=False)
    print("  ✓ InfluxDB and Grafana stopped" + (" and their data deleted" if delete else " (data kept)"))
    for image in images:
        gone = docker("rmi", image, check=False).returncode == 0
        print(f"  {'✓' if gone else '·'} image {image} " + ("removed" if gone else "(not present or in use)"))
    if delete and RUNS.exists():
        shutil.rmtree(RUNS)
        print(f"  ✓ {RUNS} deleted")
    if ours:
        link.unlink()
        print(f"  ✓ {link} removed")
    print("\nWebots, Docker and your worlds were not touched. To remove the app completely, delete this folder.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dfm", description="Emulated firmware for the drones in your Webots world")
    p.add_argument("--version", action="version", version=f"dfm {VERSION} (image {IMAGE})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="check requirements, download images and make dfm available").set_defaults(func=cmd_setup)
    check = sub.add_parser("check", help="test the installation without Webots (firmware, motors and monitoring)")
    check.add_argument("--quick", action="store_true", help="only check that Renode starts")
    check.set_defaults(func=cmd_check)
    up = sub.add_parser("up", help="find the <extern> drones in your Webots and start everything")
    up.add_argument("--port", type=int, help="Webots port, if more than one Webots is open")
    up.add_argument("--duration", type=float, help="flight seconds for each bridge (default: from the config)")
    up.add_argument("--no-monitoring", action="store_true", help="do not start InfluxDB/Grafana or the ingesters")
    up.set_defaults(func=cmd_up)
    sub.add_parser("status", help="which Webots and drones dfm sees, and what is still running").set_defaults(func=cmd_status)
    sub.add_parser("down", help="stop anything left over from a previous run").set_defaults(func=cmd_down)
    un = sub.add_parser("uninstall", help="remove what setup installed (never touches Webots, Docker or your worlds)")
    un.add_argument("--delete-data", action="store_true", help="also delete saved flights and runs/")
    un.add_argument("--dry-run", action="store_true", help="show what would be done without doing it")
    un.set_defaults(func=cmd_uninstall)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
