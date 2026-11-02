"""Renode side of the bridge: IMU stream + motor read/force via monitor -P."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)
from renode_monitor import RenodeClient  # noqa: E402

from .motor_map import MotorRatios

MOTOR_RE = re.compile(
    r"m1=(?P<m1>\d+)\s+m2=(?P<m2>\d+)\s+m3=(?P<m3>\d+)\s+m4=(?P<m4>\d+)"
)
STREAM_RE = re.compile(r"streaming=(?P<s>\d+).*virt_hz=(?P<vh>[0-9.]+)")
PREFLIGHT_RE = re.compile(r"preflight_ok=(?P<p>\d+).*state=(?P<st>\d+)")
CALIB_RE = re.compile(r"calibrated=(?P<c>\d+)")


@dataclass
class RenodeBridge:
    client: RenodeClient
    imu_hz: float = 200.0
    baro_hz: float = 50.0
    _streaming: bool = False

    @classmethod
    def connect(cls, port: int = 9999, **kwargs) -> "RenodeBridge":
        return cls(client=RenodeClient(port), **kwargs)

    def close(self) -> None:
        if self._streaming:
            try:
                self.client.query("imu_stream_stop")
            except ConnectionError:
                pass
            self._streaming = False
        self.client.close()

    def start_imu_hover(self, ax: float = 0.0, ay: float = 0.0, az: float = 1000.0) -> str:
        """Start ClockSource hover stream (static rest) — used until Webots updates."""
        out = self.client.query(
            f"imu_stream_start {self.imu_hz} 0 0 0 {ax} {ay} {az} "
            f"{self.baro_hz} 101325 25"
        )
        self._streaming = True
        return out or ""

    def set_imu_sample(
        self,
        gx: float,
        gy: float,
        gz: float,
        ax: float,
        ay: float,
        az: float,
        pressure: float = 101325.0,
        temp: float = 25.0,
    ) -> None:
        """Update live ClockSource sample (imu_stream_set) or start if needed."""
        if not self._streaming:
            out = self.client.query(
                f"imu_stream_start {self.imu_hz} {gx} {gy} {gz} {ax} {ay} {az} "
                f"{self.baro_hz} {pressure} {temp}"
            )
            self._streaming = "error=" not in (out or "")
            return
        self.client.query(
            f"imu_stream_set {gx} {gy} {gz} {ax} {ay} {az} {pressure} {temp}"
        )

    def feed_imu_burst(
        self,
        n: int,
        gx: float,
        gy: float,
        gz: float,
        ax: float,
        ay: float,
        az: float,
    ) -> str:
        return self.client.query(f"imu_burst {n} {gx} {gy} {gz} {ax} {ay} {az}") or ""

    def read_motors(self) -> MotorRatios | None:
        raw = self.client.query("motor_stats") or ""
        m = MOTOR_RE.search(raw)
        if not m:
            return None
        return MotorRatios.from_u16(
            int(m.group("m1")),
            int(m.group("m2")),
            int(m.group("m3")),
            int(m.group("m4")),
        )

    def force_motors(self, ratios: MotorRatios) -> str:
        a, b, c, d = ratios.as_u16()
        return self.client.query(f"motor_force {a} {b} {c} {d}") or ""

    def clear_motor_force(self) -> str:
        return self.client.query("motor_force_clear") or ""

    def calib_ok(self) -> bool:
        raw = self.client.query("calib_status") or ""
        m = CALIB_RE.search(raw)
        return bool(m and m.group("c") == "1")

    def preflight_ok(self) -> bool:
        raw = self.client.query("preflight_status") or ""
        m = PREFLIGHT_RE.search(raw)
        return bool(m and m.group("p") == "1")

    def inject_setpoint_rpyt(
        self,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw_rate: float = 0.0,
        thrust: float = 35000.0,
    ) -> str:
        """Refresh commander setpoint (Option C) — call ≥ ~20 Hz to beat WDT."""
        return (
            self.client.query(
                f"setpoint_rpyt {roll} {pitch} {yaw_rate} {thrust}"
            )
            or ""
        )

    def inject_setpoint_zdist(
        self,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw_rate: float = 0.0,
        z_m: float = 0.5,
    ) -> str:
        """Absolute-Z + attitude (CRTP zDistance). FW position PID owns thrust."""
        return (
            self.client.query(
                f"setpoint_zdist {roll} {pitch} {yaw_rate} {z_m}"
            )
            or ""
        )

    def inject_setpoint_hover(self, thrust: float = 35000.0) -> str:
        return self.client.query(f"setpoint_hover {thrust}") or ""

    def clear_setpoint(self) -> str:
        return self.client.query("setpoint_clear") or ""

    def setpoint_stats(self) -> str:
        return self.client.query("setpoint_stats") or ""

    def status_lines(self) -> str:
        parts = [
            self.client.query("imu_stream_status") or "",
            self.client.query("calib_status") or "",
            self.client.query("preflight_status") or "",
            self.client.query("motor_stats") or "",
            self.client.query("setpoint_stats") or "",
        ]
        return "\n".join(p for p in parts if p)
