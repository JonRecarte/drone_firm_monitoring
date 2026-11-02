"""Entrypoint of a drone_firm_monitoring Renode container (one drone).

Prepares the machine and starts Renode with its monitor on port 9999:
  - firmware: /dfm/firmware/ if dfm mounts it (a different firmware for this drone),
    otherwise the one in the image (/renode-cf/firmware/). Always cf2.elf plus its
    cf2.profile.json;
  - monitor commands: /dfm/monitor_cf.py if dfm mounts it (the repo's copy), otherwise the
    image's; they are appended to Renode's monitor.py, followed by the profile constants.
If the profile does not belong to that exact ELF (sha256), it refuses to start: with the
addresses of another binary, the bridge would write into unrelated firmware memory.
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path("/renode-cf")
RESC = """:description: Crazyflie 2.1 + canned sensors (drone_firm_monitoring)

mach create "CF2.1"
machine LoadPlatformDescription @platforms/boards/cf2.repl

sysbus.otp WriteString 0x7800 "0;CF21"
# Avoid showAnalyzer -- it can block when Renode has no console/TTY (-P mode).
sysbus LoadELF @cf2.elf

emulation CreateUARTHub "syslink"
connector Connect sysbus.usart6 syslink
connector Connect sysbus.nrf syslink
syslink Start

machine StartGdbServer 3333
emulation CreateServerSocketTerminal 3456 "sensors"
connector Connect sysbus.i2c3.bmi_gyro sensors

sysbus.i2c3.bmi_gyro FeedGyroSample 10 20 30 2
sysbus.i2c3.bmi_gyro FeedGyroSample 0 0 0 20
sysbus.i2c3.bmi_accel FeedAccSample 0 0 1000 20
sysbus.i2c3.bmp_baro FeedPTSample 101325 25 20

start
logLevel 3
"""


def first(*paths: Path) -> Path:
    return next(p for p in paths if p.exists())


def main() -> int:
    fw_dir = first(Path("/dfm/firmware/cf2.elf"), ROOT / "firmware" / "cf2.elf").parent
    elf, profile_path = fw_dir / "cf2.elf", fw_dir / "cf2.profile.json"
    if not profile_path.exists():
        print(f"[dfm-renode] {profile_path} is missing: the firmware needs its profile (firmware_profile.py)", flush=True)
        return 2
    profile = json.loads(profile_path.read_text())
    sha = hashlib.sha256(elf.read_bytes()).hexdigest()
    if profile.get("elf_sha256") != sha:
        print(f"[dfm-renode] {profile_path} belongs to another firmware ({str(profile.get('elf_sha256'))[:12]}…, ELF {sha[:12]}…)", flush=True)
        return 2
    monitor_cf = first(Path("/dfm/monitor_cf.py"), ROOT / "dfm" / "monitor_cf.py")
    overrides = "\n".join(f"{k} = {v:#x}" if v > 0xFFFF else f"{k} = {v}" for k, v in profile["constants"].items())
    (ROOT / "scripts" / "monitor.py").write_text(
        (ROOT / "scripts" / "monitor.py.orig").read_text()
        + "\n# --- swarm monitor_cf patch ---\n" + monitor_cf.read_text()
        + "\n# --- firmware profile: replaces the constants above ---\n" + overrides + "\n")
    shutil.copy(elf, ROOT / "cf2.elf")
    (ROOT / "crazyflie_canned.resc").write_text(RESC)
    print(f"[dfm-renode] firmware {sha[:12]}… from {fw_dir} · commands from {monitor_cf}", flush=True)
    os.environ["LD_LIBRARY_PATH"] = str(ROOT / "output" / "bin" / "Release")
    os.chdir(ROOT)
    os.execv("./renode", ["./renode", "--disable-xwt", "-P", "9999", "crazyflie_canned.resc"])


if __name__ == "__main__":
    sys.exit(main())
