"""
Webots I/O adapter for the official Bitcraze Crazyflie PROTO.

Devices (see Webots Crazyflie guide):
  m1_motor .. m4_motor, gyro, inertial_unit, gps

Propellers use Webots thrust/torque constants — motor velocity is the actuator
command (same mapping style as Bitcraze's crazyflie.c controller).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .frames import (
    AxisMap,
    LowPass3,
    Vec3,
    altitude_m_to_pressure_pa,
    m_s2_to_bmi_accel_units,
    rad_s_to_bmi_gyro_units,
)
from .motor_map import MotorRatios, ThrustMap


@dataclass
class SensorSample:
    gx: float
    gy: float
    gz: float
    ax: float
    ay: float
    az: float
    pressure: float
    temp: float = 25.0
    altitude_m: float = 0.0


@dataclass
class FakePlant:
    """Simple vertical integrator for bridge dry-run without Webots."""

    mass: float = 0.05  # match official Crazyflie.proto
    g: float = 9.81
    pos_z: float = 0.0
    vel_z: float = 0.0
    thrust_map: ThrustMap = field(default_factory=ThrustMap)
    timestep_s: float = 0.01
    last_acc_z: float = 0.0

    def read_sensors(self) -> SensorSample:
        az_ms2 = self.last_acc_z + self.g
        az = (az_ms2 / self.g) * 1000.0
        return SensorSample(
            gx=0.0, gy=0.0, gz=0.0,
            ax=0.0, ay=0.0, az=az,
            pressure=altitude_m_to_pressure_pa(self.pos_z),
            altitude_m=self.pos_z,
        )

    def apply_motors(self, ratios: MotorRatios) -> None:
        thrusts = self.thrust_map.ratios_to_thrusts(ratios)
        total = sum(thrusts)
        self.last_acc_z = total / self.mass - self.g
        self.vel_z += self.last_acc_z * self.timestep_s
        self.pos_z = max(0.0, self.pos_z + self.vel_z * self.timestep_s)
        if self.pos_z <= 0.0:
            self.vel_z = 0.0
            self.last_acc_z = 0.0

    def step(self) -> None:
        pass


class WebotsPlant:
    """
    Official Crazyflie PROTO plant.

    Motors: m1_motor..m4_motor with Bitcraze sign convention
      (m1/m3 negative, m2/m4 positive) so Propeller thrustConstants signs work.
    Sensors: gyro + inertial_unit + gps (no dedicated accelerometer in PROTO).
    """

    # Signs match crazyflie.c: set_velocity(m1, -p); m2 +p; m3 -p; m4 +p
    _MOTOR_SIGN = (-1.0, +1.0, -1.0, +1.0)
    _MOTOR_NAMES = ("m1_motor", "m2_motor", "m3_motor", "m4_motor")

    # Official proto thrustConstants |c0|=4e-5, mass 0.05 kg → hover ω ≈ 55 rad/s.
    # Bitcraze's PID uses ~48 as the altitude bias (see pid_controller.c).
    # Official hover ω ≈ 55 rad/s. Map FW ratio 0.55 (≈36k thrust) → ~55.
    MOTOR_OMEGA_SCALE = 100.0  # rad/s at ratio=1.0

    def __init__(
        self,
        robot,
        thrust_map: ThrustMap | None = None,
        axis_map: AxisMap | None = None,
        gyro_lpf_tau_s: float = 0.02,
    ):
        self.robot = robot
        self.thrust_map = thrust_map or ThrustMap(mass_kg=0.05, k=0.405)
        self.timestep = int(robot.getBasicTimeStep())
        self._motor_dbg = 0
        self.axis_map = axis_map or AxisMap()
        self._gyro_lpf = LowPass3(tau_s=gyro_lpf_tau_s)

        self._gyro = self._device("gyro")
        self._imu = self._device("inertial_unit")
        self._gps = self._device("gps")
        self._accel = self._device("accelerometer")  # may be absent

        self._motors = []
        for name in self._MOTOR_NAMES:
            m = self._device(name)
            self._motors.append(m)
            if m is not None:
                m.setPosition(float("inf"))
                if hasattr(m, "setAcceleration"):
                    m.setAcceleration(-1)

        if self._gyro:
            self._gyro.enable(self.timestep)
        if self._imu:
            self._imu.enable(self.timestep)
        if self._gps:
            self._gps.enable(self.timestep)
        if self._accel:
            self._accel.enable(self.timestep)

        self._node = None
        if hasattr(robot, "getSelf"):
            try:
                self._node = robot.getSelf()
            except Exception:
                self._node = None

        self._prev_gps = None
        self._prev_t = None
        self._prev_vel = None
        self._lin_acc_world = Vec3(0.0, 0.0, 0.0)
        self._last_sensor_t = None
        found = sum(1 for m in self._motors if m is not None)
        print(
            f"WebotsPlant(Crazyflie): motors={found}/4 "
            f"gyro={bool(self._gyro)} imu={bool(self._imu)} gps={bool(self._gps)} "
            f"gyro_lpf_tau={gyro_lpf_tau_s:.3f}s",
            flush=True,
        )

    def _device(self, name: str):
        try:
            return self.robot.getDevice(name)
        except Exception:
            return None

    def _altitude(self) -> float:
        if self._gps is not None:
            try:
                return float(self._gps.getValues()[2])
            except Exception:
                pass
        if self._node is not None:
            try:
                return float(self._node.getPosition()[2])
            except Exception:
                pass
        return 0.0

    def _update_lin_acc(self) -> None:
        """Finite-difference GPS velocity → world linear acceleration."""
        if self._gps is None:
            return
        try:
            t = float(self.robot.getTime())
            p = self._gps.getValues()
            if self._prev_gps is not None and self._prev_t is not None:
                dt = max(1e-4, t - self._prev_t)
                vx = (p[0] - self._prev_gps[0]) / dt
                vy = (p[1] - self._prev_gps[1]) / dt
                vz = (p[2] - self._prev_gps[2]) / dt
                if self._prev_vel is None:
                    self._prev_vel = (vx, vy, vz)
                else:
                    ax = (vx - self._prev_vel[0]) / dt
                    ay = (vy - self._prev_vel[1]) / dt
                    az = (vz - self._prev_vel[2]) / dt
                    a = 0.3
                    self._lin_acc_world = Vec3(
                        a * ax + (1 - a) * self._lin_acc_world.x,
                        a * ay + (1 - a) * self._lin_acc_world.y,
                        a * az + (1 - a) * self._lin_acc_world.z,
                    )
                    self._prev_vel = (vx, vy, vz)
            self._prev_gps = (p[0], p[1], p[2])
            self._prev_t = t
        except Exception:
            pass

    def read_sensors(self) -> SensorSample:
        g = 9.81
        self._update_lin_acc()
        alt = self._altitude()
        t = float(self.robot.getTime())
        dt = 0.0
        if self._last_sensor_t is not None:
            dt = max(0.0, t - self._last_sensor_t)
        self._last_sensor_t = t

        # Gyro: Webots body rad/s → DPS for Renode BMI088, then axis map + LPF.
        if self._gyro:
            w = self._gyro.getValues()
            gyro = rad_s_to_bmi_gyro_units(Vec3(w[0], w[1], w[2]))
            gyro = self.axis_map.apply(gyro)
            gyro = self._gyro_lpf.update(gyro, dt if dt > 0 else self.timestep_s)
        else:
            gyro = Vec3(0.0, 0.0, 0.0)

        # Accelerometer: proper acceleration in body frame (milli-g).
        nearly_still = (
            self._prev_vel is not None
            and abs(self._prev_vel[0]) < 0.05
            and abs(self._prev_vel[1]) < 0.05
            and abs(self._prev_vel[2]) < 0.05
            and abs(gyro.x) < 5.0
            and abs(gyro.y) < 5.0
            and abs(gyro.z) < 5.0
        )
        if self._accel:
            a = self._accel.getValues()
            accel = m_s2_to_bmi_accel_units(Vec3(a[0], a[1], a[2]), g=g)
        elif nearly_still:
            # Avoid GPS finite-difference spikes at rest (hurts FW estimator).
            accel = Vec3(0.0, 0.0, 1000.0)
        else:
            ax_w = self._lin_acc_world.x
            ay_w = self._lin_acc_world.y
            az_w = self._lin_acc_world.z + g
            if self._imu is not None and self._node is not None:
                try:
                    ori = self._node.getOrientation()  # body→world 3x3 row-major
                    fx = ori[0] * ax_w + ori[3] * ay_w + ori[6] * az_w
                    fy = ori[1] * ax_w + ori[4] * ay_w + ori[7] * az_w
                    fz = ori[2] * ax_w + ori[5] * ay_w + ori[8] * az_w
                    accel = m_s2_to_bmi_accel_units(Vec3(fx, fy, fz), g=g)
                except Exception:
                    accel = Vec3(0.0, 0.0, 1000.0)
            else:
                accel = Vec3(0.0, 0.0, 1000.0)
        accel = self.axis_map.apply(accel)

        return SensorSample(
            gx=gyro.x, gy=gyro.y, gz=gyro.z,
            ax=accel.x, ay=accel.y, az=accel.z,
            pressure=altitude_m_to_pressure_pa(alt),
            altitude_m=alt,
        )

    def apply_motors(self, ratios: MotorRatios) -> None:
        """FW ratios (0..1) → rad/s; signs match Bitcraze crazyflie.c set_velocity."""
        omegas = []
        for motor, ratio, sign in zip(self._motors, ratios.as_tuple(), self._MOTOR_SIGN):
            if motor is None:
                omegas.append(0.0)
                continue
            r = max(0.0, min(1.0, ratio))
            omega = sign * r * self.MOTOR_OMEGA_SCALE
            max_v = 600.0
            if hasattr(motor, "getMaxVelocity"):
                try:
                    max_v = float(motor.getMaxVelocity())
                except Exception:
                    pass
            omega = max(-max_v, min(max_v, omega))
            motor.setVelocity(omega)
            omegas.append(omega)
        if self._motor_dbg < 3:
            print(
                f"WebotsPlant ω=({omegas[0]:.0f},{omegas[1]:.0f},"
                f"{omegas[2]:.0f},{omegas[3]:.0f}) "
                f"r=({ratios.m1:.2f},{ratios.m2:.2f},{ratios.m3:.2f},{ratios.m4:.2f})",
                flush=True,
            )
            self._motor_dbg += 1

    def step(self) -> int:
        return self.robot.step(self.timestep)

    @property
    def timestep_s(self) -> float:
        return self.timestep * 1e-3

    def step_for(self, duration_s: float) -> int:
        """Advance simulation by approximately duration_s (multiple basic steps)."""
        n = max(1, int(round(duration_s / self.timestep_s)))
        last = 0
        for _ in range(n):
            last = self.step()
            if last == -1:
                break
        return last

    def motor_velocities(self) -> tuple[float, float, float, float]:
        out = []
        for m in self._motors:
            if m is None:
                out.append(0.0)
                continue
            try:
                out.append(float(m.getVelocity()))
            except Exception:
                out.append(0.0)
        while len(out) < 4:
            out.append(0.0)
        return (out[0], out[1], out[2], out[3])

    def unstick_for_takeoff(self, xyz=None, force: bool = False) -> None:
        """
        After a long sit (or a crash), floor contact can lock the craft so
        propeller thrust never lifts it. Nudge slightly above the floor.
        Re-allowed when force=True or after we have been near the floor again.
        Without xyz the craft keeps its own x, y (several drones share one world).
        """
        if self._node is None:
            return
        if getattr(self, "_unstuck", False) and not force:
            return
        try:
            field = self._node.getField("translation")
            if field is not None:
                if xyz is None:
                    x, y, _ = field.getSFVec3f()
                    xyz = (x, y, 0.18)
                field.setSFVec3f(list(xyz))
            rot = self._node.getField("rotation")
            if rot is not None:
                rot.setSFRotation([0, 0, 1, 0])
            # Clear residual velocity if supervisor API allows.
            if hasattr(self._node, "resetPhysics"):
                try:
                    self._node.resetPhysics()
                except Exception:
                    pass
            self._unstuck = True
            print(f"WebotsPlant: unstick_for_takeoff {xyz}", flush=True)
        except Exception as exc:
            print(f"WebotsPlant: unstick failed ({exc})", flush=True)

    def maybe_allow_reunstick(self, alt_m: float) -> None:
        """After settling back on the floor, permit another unstick nudge."""
        if alt_m < 0.05:
            self._floor_sit = getattr(self, "_floor_sit", 0) + 1
            if self._floor_sit > 25:  # ~2 s at 12 Hz
                self._unstuck = False
                self._floor_sit = 0
        else:
            self._floor_sit = 0


def open_plant(
    prefer_webots: bool = True,
    thrust_map: ThrustMap | None = None,
    axis_map: AxisMap | None = None,
    gyro_lpf_tau_s: float = 0.02,
):
    if prefer_webots:
        try:
            try:
                from controller import Supervisor  # type: ignore

                robot = Supervisor()
            except Exception:
                from controller import Robot  # type: ignore

                robot = Robot()
            return WebotsPlant(
                robot,
                thrust_map=thrust_map,
                axis_map=axis_map,
                gyro_lpf_tau_s=gyro_lpf_tau_s,
            )
        except Exception as exc:
            print(f"open_plant: Webots unavailable ({exc}); FakePlant", flush=True)
    tm = thrust_map or ThrustMap(mass_kg=0.05, k=0.49)
    return FakePlant(thrust_map=tm, mass=tm.mass_kg)
