#!/usr/bin/env python3
"""Builds the profile of a cf2.elf: the addresses and offsets that monitor_cf.py uses.

monitor_cf.py reads and writes firmware variables by their address in RAM. Those
addresses change with every firmware version (or compiler), so they are taken from the
ELF itself with gdb, using its debug information, instead of being written by hand. The
profile records the ELF's sha256: it is only used with that exact binary.

    firmware_profile.py cf2.elf                  # writes cf2.profile.json next to the ELF
    firmware_profile.py cf2.elf -o profile.json  # or wherever you say

Needs gdb with ARM support (gdb-multiarch). Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

FORMAT = 1

BIAS = "'sensors_bmi088_bmp3xx.c'::gyroBiasRunning"   # another driver has a copy with the same name
SETPOINT = "'stabilizer.c'::setpoint"
SUP = "supervisorMem"


def addr(sym: str) -> str:
    return f"(unsigned long)&{sym}"


def off(base: str, member: str) -> str:
    return f"(long)((char*)&{base}.{member} - (char*)&{base})"


# monitor_cf.py constant -> gdb expression giving its value in this ELF.
CONSTANTS = {
    "_GYRO_BIAS_FOUND_ADDR": addr("'sensors_bmi088_bmp3xx.c'::gyroBiasFound"),
    "_GYRO_BIAS_RUNNING_ADDR": addr(BIAS),
    "_BIAS_IS_FOUND_OFF": off(BIAS, "isBiasValueFound"),
    "_BIAS_IS_FILLED_OFF": off(BIAS, "isBufferFilled"),
    "_SUPERVISOR_MEM_ADDR": addr(SUP),
    "_SUP_OFF_CAN_FLY": off(SUP, "canFly"),
    "_SUP_OFF_IS_FLYING": off(SUP, "isFlying"),
    "_SUP_OFF_IS_TUMBLED": off(SUP, "isTumbled"),
    "_SUP_OFF_IS_ARMED": off(SUP, "isArmingActivated"),
    "_SUP_OFF_IS_CRASHED": off(SUP, "isCrashed"),
    "_SUP_OFF_INFO": off(SUP, "infoBitfield"),
    "_SUP_OFF_STATE": off(SUP, "state"),
    "_SUP_OFF_CONDITIONS": off(SUP, "latestConditions"),
    "_MOTOR_RATIOS_ADDR": addr("motor_ratios"),
    "_MOTOR_POWER_SET_ADDR": addr("motorPowerSet"),
    "_MOTOR_SET_ENABLE_ADDR": addr("motorSetEnable"),
    "_SETPOINT_QUEUE_STORAGE": addr("osSys_setpointQueueStorage"),
    "_PRIORITY_QUEUE_STORAGE": addr("osSys_priorityQueueStorage"),
    "_XTICK_COUNT_ADDR": addr("xTickCount"),
    "_STABILIZER_SETPOINT": addr(SETPOINT),
    "_SETPOINT_SIZE": "(long)sizeof(setpoint_t)",
    "_SP_OFF_TIMESTAMP": off(SETPOINT, "timestamp"),
    "_SP_OFF_ATT_ROLL": off(SETPOINT, "attitude.roll"),
    "_SP_OFF_ATT_PITCH": off(SETPOINT, "attitude.pitch"),
    "_SP_OFF_ATT_YAW": off(SETPOINT, "attitude.yaw"),
    "_SP_OFF_RATE_YAW": off(SETPOINT, "attitudeRate.yaw"),
    "_SP_OFF_THRUST": off(SETPOINT, "thrust"),
    "_SP_OFF_POS_X": off(SETPOINT, "position.x"),
    "_SP_OFF_POS_Y": off(SETPOINT, "position.y"),
    "_SP_OFF_POS_Z": off(SETPOINT, "position.z"),
    "_SP_OFF_VEL_X": off(SETPOINT, "velocity.x"),
    "_SP_OFF_VEL_Y": off(SETPOINT, "velocity.y"),
    "_SP_OFF_VEL_Z": off(SETPOINT, "velocity.z"),
    "_SP_OFF_VEL_BODY": off(SETPOINT, "velocity_body"),
    "_SP_OFF_MODE_X": off(SETPOINT, "mode.x"),
    "_SP_OFF_MODE_Y": off(SETPOINT, "mode.y"),
    "_SP_OFF_MODE_Z": off(SETPOINT, "mode.z"),
    "_SP_OFF_MODE_ROLL": off(SETPOINT, "mode.roll"),
    "_SP_OFF_MODE_PITCH": off(SETPOINT, "mode.pitch"),
    "_SP_OFF_MODE_YAW": off(SETPOINT, "mode.yaw"),
    "_MODE_DISABLE": "(long)modeDisable",
    "_MODE_ABS": "(long)modeAbs",
    "_MODE_VELOCITY": "(long)modeVelocity",
}

# Values that monitor_cf.py hard-codes in its logic (not as constants): if the firmware
# changes them, no profile is produced, because that code would no longer be right.
FIXED = {
    "supervisorStatePreFlChecksPassed": 2,
    "supervisorStateArming": 3,
    "supervisorStateReadyToFly": 4,
    "supervisorStateFlying": 5,
    "supervisorStateLanded": 6,
    "supervisorStateWarningLevelOut": 8,
    "supervisorConditionIsTumbled": 2,
    "supervisorConditionDeckFault": 9,
}


def run_gdb(gdb: str, elf: Path, exprs: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    """Evaluates each expression on its own: if a symbol is missing, gdb goes on with the rest."""
    args = [gdb, "-batch", "-nx"]
    for name, expr in exprs.items():
        args += ["-ex", f'printf "DFM {name} %ld\\n", {expr}']
    res = subprocess.run(args + [str(elf)], capture_output=True, text=True)
    values = {}
    for line in res.stdout.splitlines():
        if line.startswith("DFM "):
            _, name, value = line.split()
            values[name] = int(value)
    errors = [ln for ln in res.stderr.splitlines() if ln.strip()]
    return values, errors


def build(elf: Path, gdb: str) -> dict:
    exprs = dict(CONSTANTS)
    exprs.update({k: f"(long){k}" for k in FIXED})
    exprs["_QUEUE_STORAGE_SIZE"] = "(long)sizeof(osSys_setpointQueueStorage)"
    values, errors = run_gdb(gdb, elf, exprs)
    missing = [k for k in exprs if k not in values]
    if missing:
        sys.exit("Could not read from the ELF: " + ", ".join(missing) + "\n" + "\n".join(errors[:10]))
    changed = {k: (values[k], v) for k, v in FIXED.items() if values[k] != v}
    if changed:
        sys.exit("This firmware changes values that monitor_cf.py assumes are fixed: "
                 + ", ".join(f"{k}={a} (expected {b})" for k, (a, b) in changed.items()))
    if values["_QUEUE_STORAGE_SIZE"] != values["_SETPOINT_SIZE"]:
        sys.exit("The setpoint queue does not hold exactly one setpoint_t: monitor_cf.py assumes a queue of length 1")
    version = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    return {
        "format": FORMAT,
        "elf": elf.name,
        "elf_sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gdb": version,
        "constants": {k: values[k] for k in CONSTANTS},
        "checked": {k: values[k] for k in FIXED},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Profile (addresses and offsets) of a cf2.elf for monitor_cf.py")
    p.add_argument("elf", type=Path)
    p.add_argument("-o", "--output", type=Path, help="default: <elf without extension>.profile.json next to the ELF")
    p.add_argument("--gdb", default="gdb-multiarch")
    args = p.parse_args(argv)
    profile = build(args.elf, args.gdb)
    out = args.output or args.elf.with_suffix(".profile.json")
    out.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(profile['constants'])} constants -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
