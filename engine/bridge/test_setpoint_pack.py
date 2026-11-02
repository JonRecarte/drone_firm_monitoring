#!/usr/bin/env python3
"""Sanity check RPYT setpoint packing (no Renode required)."""
from __future__ import annotations

import struct
import sys

from bridge.setpoint import (
    MODE_ABS,
    MODE_DISABLE,
    MODE_VELOCITY,
    OFF_MODE_PITCH,
    OFF_MODE_ROLL,
    OFF_MODE_YAW,
    OFF_MODE_Z,
    OFF_POS_Z,
    OFF_THRUST,
    RpytSetpoint,
    SETPOINT_SIZE,
    ZdistSetpoint,
)


def main() -> int:
    sp = RpytSetpoint(roll_deg=1.5, pitch_deg=-2.0, yaw_rate_deg_s=10.0, thrust=35000, timestamp=12345)
    blob = sp.pack()
    assert len(blob) == SETPOINT_SIZE
    assert struct.unpack_from("<f", blob, OFF_THRUST)[0] == 35000.0
    assert blob[OFF_MODE_Z] == MODE_DISABLE
    assert blob[OFF_MODE_ROLL] == MODE_ABS
    assert blob[OFF_MODE_PITCH] == MODE_ABS
    assert blob[OFF_MODE_YAW] == MODE_VELOCITY
    back = RpytSetpoint.unpack(blob)
    assert abs(back.roll_deg - 1.5) < 1e-5
    assert abs(back.pitch_deg - (-2.0)) < 1e-5
    assert abs(back.yaw_rate_deg_s - 10.0) < 1e-5
    assert abs(back.thrust - 35000.0) < 1e-3
    assert back.timestamp == 12345

    zd = ZdistSetpoint(roll_deg=0.0, pitch_deg=0.0, yaw_rate_deg_s=0.0, z_m=0.5, timestamp=99)
    zb = zd.pack()
    assert zb[OFF_MODE_Z] == MODE_ABS
    assert abs(struct.unpack_from("<f", zb, OFF_POS_Z)[0] - 0.5) < 1e-6
    zb2 = ZdistSetpoint.unpack(zb)
    assert abs(zb2.z_m - 0.5) < 1e-6
    print("ok setpoint pack/unpack (rpyt + zdist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
