"""Pack Crazyflie setpoint_t blobs (host-side helpers / tests)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

# Must match scripts/monitor_cf.py / cf2.elf (-fshort-enums); DWARF-verified.
SETPOINT_SIZE = 0x80
MODE_DISABLE = 0
MODE_ABS = 1
MODE_VELOCITY = 2

OFF_TIMESTAMP = 0
OFF_ATT_ROLL = 8
OFF_ATT_PITCH = 12
OFF_ATT_YAW = 16
OFF_RATE_YAW = 32
OFF_THRUST = 52
OFF_POS_X = 60
OFF_POS_Y = 64
OFF_POS_Z = 68
OFF_VEL_X = 76
OFF_VEL_Y = 80
OFF_VEL_Z = 84
OFF_VEL_BODY = 120
OFF_MODE_X = 121
OFF_MODE_Y = 122
OFF_MODE_Z = 123
OFF_MODE_ROLL = 124
OFF_MODE_PITCH = 125
OFF_MODE_YAW = 126


@dataclass
class RpytSetpoint:
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_rate_deg_s: float = 0.0
    thrust: float = 35000.0  # 0..60000, CF float thrust units
    timestamp: int = 0

    def pack(self) -> bytes:
        buf = bytearray(SETPOINT_SIZE)
        struct.pack_into("<I", buf, OFF_TIMESTAMP, self.timestamp & 0xFFFFFFFF)
        struct.pack_into("<f", buf, OFF_ATT_ROLL, float(self.roll_deg))
        struct.pack_into("<f", buf, OFF_ATT_PITCH, float(self.pitch_deg))
        struct.pack_into("<f", buf, OFF_ATT_YAW, 0.0)
        # Legacy RPYT inverts yaw rate when decoding
        struct.pack_into("<f", buf, OFF_RATE_YAW, -float(self.yaw_rate_deg_s))
        thr = max(0.0, min(60000.0, float(self.thrust)))
        struct.pack_into("<f", buf, OFF_THRUST, thr)
        buf[OFF_VEL_BODY] = 0
        buf[OFF_MODE_X] = MODE_DISABLE
        buf[OFF_MODE_Y] = MODE_DISABLE
        buf[OFF_MODE_Z] = MODE_DISABLE
        buf[OFF_MODE_ROLL] = MODE_ABS
        buf[OFF_MODE_PITCH] = MODE_ABS
        buf[OFF_MODE_YAW] = MODE_VELOCITY
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> "RpytSetpoint":
        if len(data) < SETPOINT_SIZE:
            raise ValueError("setpoint blob too short")
        ts = struct.unpack_from("<I", data, OFF_TIMESTAMP)[0]
        roll = struct.unpack_from("<f", data, OFF_ATT_ROLL)[0]
        pitch = struct.unpack_from("<f", data, OFF_ATT_PITCH)[0]
        yaw_rate = -struct.unpack_from("<f", data, OFF_RATE_YAW)[0]
        thrust = struct.unpack_from("<f", data, OFF_THRUST)[0]
        return cls(roll, pitch, yaw_rate, thrust, ts)


@dataclass
class ZdistSetpoint:
    """CRTP zDistance: absolute Z + roll/pitch abs + yaw rate."""

    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_rate_deg_s: float = 0.0
    z_m: float = 0.5
    timestamp: int = 0

    def pack(self) -> bytes:
        buf = bytearray(SETPOINT_SIZE)
        struct.pack_into("<I", buf, OFF_TIMESTAMP, self.timestamp & 0xFFFFFFFF)
        struct.pack_into("<f", buf, OFF_ATT_ROLL, float(self.roll_deg))
        struct.pack_into("<f", buf, OFF_ATT_PITCH, float(self.pitch_deg))
        struct.pack_into("<f", buf, OFF_ATT_YAW, 0.0)
        struct.pack_into("<f", buf, OFF_RATE_YAW, -float(self.yaw_rate_deg_s))
        struct.pack_into("<f", buf, OFF_THRUST, 0.0)
        struct.pack_into("<f", buf, OFF_POS_X, 0.0)
        struct.pack_into("<f", buf, OFF_POS_Y, 0.0)
        struct.pack_into("<f", buf, OFF_POS_Z, float(self.z_m))
        buf[OFF_VEL_BODY] = 0
        buf[OFF_MODE_X] = MODE_DISABLE
        buf[OFF_MODE_Y] = MODE_DISABLE
        buf[OFF_MODE_Z] = MODE_ABS
        buf[OFF_MODE_ROLL] = MODE_ABS
        buf[OFF_MODE_PITCH] = MODE_ABS
        buf[OFF_MODE_YAW] = MODE_VELOCITY
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> "ZdistSetpoint":
        if len(data) < SETPOINT_SIZE:
            raise ValueError("setpoint blob too short")
        ts = struct.unpack_from("<I", data, OFF_TIMESTAMP)[0]
        roll = struct.unpack_from("<f", data, OFF_ATT_ROLL)[0]
        pitch = struct.unpack_from("<f", data, OFF_ATT_PITCH)[0]
        yaw_rate = -struct.unpack_from("<f", data, OFF_RATE_YAW)[0]
        z_m = struct.unpack_from("<f", data, OFF_POS_Z)[0]
        return cls(roll, pitch, yaw_rate, z_m, ts)
