"""Map Crazyflie motor ratios <-> Webots propeller thrust."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MotorRatios:
    m1: float  # 0..1
    m2: float
    m3: float
    m4: float

    @classmethod
    def from_u16(cls, a: int, b: int, c: int, d: int) -> "MotorRatios":
        s = 65535.0
        return cls(a / s, b / s, c / s, d / s)

    def as_u16(self) -> tuple[int, int, int, int]:
        def clip(x: float) -> int:
            v = int(round(x * 65535.0))
            return max(0, min(65535, v))

        return clip(self.m1), clip(self.m2), clip(self.m3), clip(self.m4)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.m1, self.m2, self.m3, self.m4)

    def mean(self) -> float:
        return 0.25 * (self.m1 + self.m2 + self.m3 + self.m4)

    def as_collective(self) -> "MotorRatios":
        """Equal ratios on all motors (no roll/pitch/yaw mixing)."""
        r = self.mean()
        return MotorRatios(r, r, r, r)


@dataclass
class ThrustMap:
    """thrust_N = k * ratio^2  (per motor). Tune k so 4*k*(hover_ratio)^2 ~= mg."""

    # Match official Crazyflie.proto mass 0.05 kg; hover near ratio≈0.55
    # k = mg / (4 * r^2) with r=0.55 → ≈ 0.405
    k: float = 0.405
    mass_kg: float = 0.05
    gravity: float = 9.81

    def ratio_to_thrust_n(self, ratio: float) -> float:
        r = max(0.0, min(1.0, ratio))
        return self.k * r * r

    def ratios_to_thrusts(self, ratios: MotorRatios) -> tuple[float, float, float, float]:
        return (
            self.ratio_to_thrust_n(ratios.m1),
            self.ratio_to_thrust_n(ratios.m2),
            self.ratio_to_thrust_n(ratios.m3),
            self.ratio_to_thrust_n(ratios.m4),
        )

    def hover_ratio(self) -> float:
        # 4 * k * r^2 = m g  => r = sqrt(mg / (4k))
        return (self.mass_kg * self.gravity / (4.0 * self.k)) ** 0.5
