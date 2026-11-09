"""Renode side of the bridge: IMU stream + motor read/force via monitor -P."""
from __future__ import annotations

import os
import re
import sys
import time
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
VIRT_RE = re.compile(r"virt=(?P<v>[0-9.]+)")
# Host time reported by lockstep_step (monitor_cf.py), in ms.
STEP_TIMING_RE = re.compile(
    r"pre_ms=(?P<pre>[0-9.]+)\s+run_ms=(?P<run>[0-9.]+)\s+cb_ms=(?P<cb>[0-9.]+)\s+cb_n=(?P<n>\d+)"
)


# The firmware's gyro self-test fails on a reading of exactly (0, 0, 0), which a real sensor
# never gives. Every gyro sample therefore carries this small constant offset, like the zero-rate
# offset of a real BMI088 (up to about ±1 °/s); the firmware's calibration measures and removes it.
GYRO_OFFSET_DPS = (0.5, -0.4, 0.3)


def _gyro(gx: float, gy: float, gz: float) -> tuple[float, float, float]:
    ox, oy, oz = GYRO_OFFSET_DPS
    return gx + ox, gy + oy, gz + oz


def _nums(*values: float) -> str:
    """Numbers for a monitor command, always in fixed-point: the monitor splits scientific
    notation ("1e-17" -> "1", "e-17"), so the command gets one argument too many and fails.
    A near-zero gyro rate is enough to trigger it (measured 4 Nov 2026)."""
    return " ".join(f"{float(v):.6f}" for v in values)


@dataclass
class RenodeBridge:
    client: RenodeClient
    imu_hz: float = 200.0
    baro_hz: float = 50.0
    _streaming: bool = False

    @classmethod
    def connect(cls, port: int = 9999, client_opts: dict | None = None, **kwargs) -> "RenodeBridge":
        return cls(client=RenodeClient(port, **(client_opts or {})), **kwargs)

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
            f"imu_stream_start {_nums(self.imu_hz, *_gyro(0, 0, 0), ax, ay, az, self.baro_hz, 101325, 25)}"
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
                f"imu_stream_start {_nums(self.imu_hz, *_gyro(gx, gy, gz), ax, ay, az, self.baro_hz, pressure, temp)}"
            )
            self._streaming = "error=" not in (out or "")
            return
        self.client.query(
            f"imu_stream_set {_nums(*_gyro(gx, gy, gz), ax, ay, az, pressure, temp)}"
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
        return self.client.query(f"imu_burst {int(n)} {_nums(*_gyro(gx, gy, gz), ax, ay, az)}") or ""

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
                f"setpoint_rpyt {_nums(roll, pitch, yaw_rate, thrust)}"
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
                f"setpoint_zdist {_nums(roll, pitch, yaw_rate, z_m)}"
            )
            or ""
        )

    def inject_setpoint_hover(self, thrust: float = 35000.0) -> str:
        return self.client.query(f"setpoint_hover {_nums(thrust)}") or ""

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

    # --- Lockstep: the emulation stays paused and only advances when the bridge asks ---

    def pause(self) -> str:
        return self.client.query("pause") or ""

    def set_quantum(self, us: int) -> str:
        return self.client.query(f"quantum {int(us)}") or ""

    def run_for(self, seconds: float) -> str:
        """Advance the paused emulation by exactly `seconds` of virtual time (blocking)."""
        return self.client.query(f'emulation RunFor "{seconds:g}"') or ""

    def virtual_time(self) -> float:
        m = VIRT_RE.search(self.client.query("cpu_stats") or "")
        return float(m.group("v")) if m else float("nan")

    def lockstep_step(
        self, roll: float, pitch: float, yaw_rate: float, thrust: float, sample, dt_s: float
    ) -> tuple[MotorRatios, float]:
        """Sensor sample + RPYT setpoint, then exactly dt_s of firmware time, in one query.
        Returns the motor ratios at the end of the step and the virtual time reached."""
        t0 = time.perf_counter()
        raw = self.client.query(
            f"lockstep_step {_nums(roll, pitch, yaw_rate, thrust)} "
            f"{_nums(*_gyro(sample.gx, sample.gy, sample.gz), sample.ax, sample.ay, sample.az, sample.pressure)} "
            f"{int(round(dt_s * 1e6))}"
        ) or ""
        q_ms = (time.perf_counter() - t0) * 1000.0
        m, v = MOTOR_RE.search(raw), VIRT_RE.search(raw)
        if not m or not v:
            raise RuntimeError(f"lockstep_step failed: {raw!r}")
        # Where the time of the call went (host ms): the whole round trip, and inside Renode
        # the setup, RunFor and the IMU callbacks within RunFor.
        self.last_step_timing = {"rn_query_ms": q_ms}
        t = STEP_TIMING_RE.search(raw)
        if t:
            self.last_step_timing.update(
                rn_pre_ms=float(t.group("pre")),
                rn_run_ms=float(t.group("run")),
                rn_cb_ms=float(t.group("cb")),
                rn_cb_n=float(t.group("n")),
            )
        ratios = MotorRatios.from_u16(
            int(m.group("m1")), int(m.group("m2")), int(m.group("m3")), int(m.group("m4"))
        )
        return ratios, float(v.group("v"))
