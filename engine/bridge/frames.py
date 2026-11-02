"""Coordinate frame helpers: Webots body ↔ Crazyflie / Renode BMI088 units.

Renode BMI088 models (Bitcraze fork):
  FeedGyroSample(x,y,z)  — degrees/second (DPStoByte)
  FeedAccSample(x,y,z)   — milli-g (1000 ≈ 1 g upright on Z)

Webots Gyro/Accelerometer (body-mounted on Crazyflie PROTO):
  getValues() — rad/s and m/s² in the robot body frame (X fwd, Y left, Z up).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Vec3:
    x: float
    y: float
    z: float


@dataclass
class AxisMap:
    """Permute/flip body axes before feeding Renode (identity = Webots CF PROTO)."""

    # Output axis index for each input (0=x,1=y,2=z) and sign.
    # Default: no remap.
    sx: float = 1.0
    sy: float = 1.0
    sz: float = 1.0
    # Optional swap: if True, exchange X and Y after signs (rarely needed).
    swap_xy: bool = False

    def apply(self, v: Vec3) -> Vec3:
        x, y, z = self.sx * v.x, self.sy * v.y, self.sz * v.z
        if self.swap_xy:
            x, y = y, x
        return Vec3(x, y, z)


@dataclass
class LowPass3:
    """First-order vector LPF. alpha = dt/(tau+dt); higher alpha → less smoothing."""

    tau_s: float = 0.02
    y: Vec3 = field(default_factory=lambda: Vec3(0.0, 0.0, 0.0))
    _init: bool = False

    def reset(self, v: Vec3 | None = None) -> None:
        self.y = v or Vec3(0.0, 0.0, 0.0)
        self._init = v is not None

    def update(self, v: Vec3, dt_s: float) -> Vec3:
        if not self._init or dt_s <= 0.0:
            self.y = Vec3(v.x, v.y, v.z)
            self._init = True
            return self.y
        a = dt_s / (self.tau_s + dt_s)
        self.y = Vec3(
            self.y.x + a * (v.x - self.y.x),
            self.y.y + a * (v.y - self.y.y),
            self.y.z + a * (v.z - self.y.z),
        )
        return self.y


def quat_rotate(q: tuple[float, float, float, float], v: Vec3) -> Vec3:
    """Rotate vector by quaternion (x,y,z,w) — Webots convention."""
    x, y, z, w = q
    tx = 2.0 * (y * v.z - z * v.y)
    ty = 2.0 * (z * v.x - x * v.z)
    tz = 2.0 * (x * v.y - y * v.x)
    return Vec3(
        v.x + w * tx + (y * tz - z * ty),
        v.y + w * ty + (z * tx - x * tz),
        v.z + w * tz + (x * ty - y * tx),
    )


def enu_accel_to_cf_body(
    lin_acc_world: Vec3,
    gravity_world: Vec3,
    orientation_xyzw: tuple[float, float, float, float],
) -> Vec3:
    """
    Specific force in body frame for BMI088-style accel (includes gravity).
    Webots: world ENU, +Z up. CF body: +Z up when upright.
    """
    fx = lin_acc_world.x - gravity_world.x
    fy = lin_acc_world.y - gravity_world.y
    fz = lin_acc_world.z - gravity_world.z
    x, y, z, w = orientation_xyzw
    q_conj = (-x, -y, -z, w)
    return quat_rotate(q_conj, Vec3(fx, fy, fz))


def m_s2_to_bmi_accel_units(a: Vec3, g: float = 9.81) -> Vec3:
    """Map m/s² specific force → Renode FeedAccSample milli-g (1000 ≈ 1 g)."""
    scale = 1000.0 / g
    return Vec3(a.x * scale, a.y * scale, a.z * scale)


def rad_s_to_bmi_gyro_units(w: Vec3) -> Vec3:
    """Map rad/s → Renode FeedGyroSample deg/s (BMI088_Gyroscope.DPStoByte)."""
    r2d = 180.0 / math.pi
    return Vec3(w.x * r2d, w.y * r2d, w.z * r2d)


def altitude_m_to_pressure_pa(alt_m: float, p0: float = 101325.0) -> float:
    """Simple barometric formula (ISA troposphere approx)."""
    return p0 * (1.0 - 2.25577e-5 * alt_m) ** 5.25588


def pressure_pa_to_cf_asl(pressure_pa: float) -> float:
    """
    Crazyflie sensorsScaleBaro ASL (m), matching sensors_bmi088_bmp3xx.c:
      pressure_hPa = pressure_Pa * 0.01
      asl = ((1015.7 / p)^0.1902630958 - 1) * 145366.45 * 0.3048
    """
    p = max(1e-3, float(pressure_pa) * 0.01)
    return ((1015.7 / p) ** 0.1902630958 - 1.0) * 145366.45 * 0.3048
