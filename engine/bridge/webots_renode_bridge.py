#!/usr/bin/env python3
"""
Host bridge: plant (Webots or FakePlant) <-> Renode Crazyflie.

Modes:
  sensors-only     — plant IMU -> Renode; motors ignored
  motors-open-loop — force motor ratios; plant integrates thrust; IMU from plant
  closed-loop      — FW attitude + altitude → m1..m4 → plant; sensors back

Closed-loop default (--alt-hold): host PD maps plant altitude -> RPYT thrust
(commander role); firmware attitude mixer owns m1..m4 applied 1:1. IMU/baro
from the plant close the FW loops.

Optional --fw-zdist: CRTP Absolute-Z setpoint (FW position PID owns thrust).
See PURE_FW_LOOP.md.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bridge.frames import AxisMap, pressure_pa_to_cf_asl  # noqa: E402
from bridge.motor_map import MotorRatios, ThrustMap  # noqa: E402
from bridge.renode_io import RenodeBridge  # noqa: E402
from bridge.telemetry import CycleSample, TelemetryWriter  # noqa: E402
from bridge.webots_io import FakePlant, open_plant  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Webots <-> Renode CF bridge")
    p.add_argument("--port", type=int, default=int(os.environ.get("RENODE_MONITOR_PORT", "9999")))
    p.add_argument(
        "--mode",
        choices=("sensors-only", "motors-open-loop", "closed-loop"),
        default="sensors-only",
    )
    p.add_argument("--fake", action="store_true", help="Use FakePlant (no Webots)")
    p.add_argument("--imu-hz", type=float, default=100.0)
    p.add_argument("--baro-hz", type=float, default=50.0)
    p.add_argument("--loop-hz", type=float, default=20.0, help="Bridge loop rate (wall)")
    p.add_argument("--seconds", type=float, default=0.0, help="Stop after N seconds (0=forever)")
    p.add_argument(
        "--force-ratio",
        type=float,
        default=0.0,
        help="motors-open-loop base ratio (0=use ThrustMap.hover_ratio)",
    )
    p.add_argument(
        "--thrust",
        type=float,
        default=30500.0,
        help="RPYT-only base thrust (ignored under default --alt-hold)",
    )
    p.add_argument("--roll", type=float, default=0.0)
    p.add_argument("--pitch", type=float, default=0.0)
    p.add_argument("--yaw-rate", type=float, default=0.0)
    p.add_argument(
        "--alt-hold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Host PD altitude via thrust setpoint (default on). FW still mixes motors.",
    )
    p.add_argument(
        "--fw-zdist",
        action="store_true",
        help="FW Absolute-Z (setpoint_zdist); overrides host thrust PD",
    )
    p.add_argument(
        "--alt-target",
        type=float,
        default=0.5,
        help="Altitude target (m)",
    )
    p.add_argument("--alt-kp", type=float, default=0.40, help="Altitude P gain (thrust)")
    p.add_argument("--alt-kd", type=float, default=1.10, help="Altitude D gain")
    p.add_argument("--alt-ki", type=float, default=0.04, help="Altitude I gain")
    p.add_argument(
        "--hover-thrust",
        type=float,
        default=30000.0,
        help="Nominal hover thrust for host alt-hold bias",
    )
    p.add_argument(
        "--gyro-sign",
        type=str,
        default="1,1,1",
        help="Gyro/accel axis signs as sx,sy,sz (e.g. 1,-1,1 to flip pitch rate)",
    )
    p.add_argument("--gyro-lpf-tau", type=float, default=0.02, help="Gyro LPF time constant (s)")
    p.add_argument("--status-every", type=float, default=2.0)
    p.add_argument(
        "--telemetry",
        type=str,
        default="",
        help="Append JSONL cycle samples here (probe/orchestration tap)",
    )
    p.add_argument("--wait-calib", action="store_true")
    p.add_argument("--wait-preflight", action="store_true")
    p.add_argument(
        "--gate-timeout",
        type=float,
        default=180.0,
        help="Wall seconds to wait for calib/preflight gates (slow hosts boot in minutes)",
    )
    return p.parse_args()


def _parse_gyro_sign(s: str) -> AxisMap:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 3:
        raise SystemExit(f"--gyro-sign needs sx,sy,sz got {s!r}")
    return AxisMap(sx=float(parts[0]), sy=float(parts[1]), sz=float(parts[2]))


def _push_sensors(renode: RenodeBridge, sample, zero_gyro: bool = False) -> None:
    if zero_gyro:
        gx = gy = gz = 0.0
    else:
        gx, gy, gz = sample.gx, sample.gy, sample.gz
    renode.set_imu_sample(
        gx, gy, gz,
        sample.ax, sample.ay, sample.az,
        sample.pressure, sample.temp,
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def main() -> int:
    args = parse_args()
    axis_map = _parse_gyro_sign(args.gyro_sign)
    fw_zdist = bool(args.fw_zdist) and args.mode == "closed-loop"
    host_alt = bool(args.alt_hold) and args.mode == "closed-loop" and not fw_zdist
    hold_alt_ol = args.alt_target >= 0.0 and args.mode == "motors-open-loop"
    telem: TelemetryWriter | None = None
    print(
        f"bridge mode={args.mode} port={args.port} fake={args.fake} "
        f"imu_hz={args.imu_hz} loop_hz={args.loop_hz} "
        f"alt_hold={host_alt} fw_zdist={fw_zdist} "
        f"alt_target={args.alt_target} gyro_sign={args.gyro_sign}",
        flush=True,
    )

    thrust_map = ThrustMap()
    hover_r = thrust_map.hover_ratio()
    base_ratio = args.force_ratio if args.force_ratio > 0 else hover_r

    print(f"Connecting Renode monitor :{args.port} ...", flush=True)
    renode = RenodeBridge.connect(args.port, imu_hz=args.imu_hz, baro_hz=args.baro_hz)
    print("Renode connected.", flush=True)

    plant = (
        FakePlant(timestep_s=1.0 / args.loop_hz, thrust_map=thrust_map)
        if args.fake
        else open_plant(
            prefer_webots=True,
            thrust_map=thrust_map,
            axis_map=axis_map,
            gyro_lpf_tau_s=args.gyro_lpf_tau,
        )
    )
    if isinstance(plant, FakePlant):
        print("Using FakePlant (no Webots controller module).", flush=True)
    else:
        print(
            f"Using WebotsPlant axis=({axis_map.sx},{axis_map.sy},{axis_map.sz})",
            flush=True,
        )

    period = 1.0 / args.loop_hz if args.loop_hz > 0 else 0.05
    imu_period = 1.0 / min(args.imu_hz, 40.0) if args.imu_hz > 0 else 0.025
    prev_alt = 0.0
    prev_t = time.time()
    use_webots = not isinstance(plant, FakePlant)

    def _sim_time() -> float:
        robot = getattr(plant, "robot", None)
        if robot is None:
            return 0.0
        return float(robot.getTime())

    def _catch_up(wall_origin: float, sim_origin: float, n_steps: list[int]) -> bool:
        if not use_webots:
            return True
        target = sim_origin + (time.time() - wall_origin)
        deadline = _sim_time() + 0.25
        while _sim_time() < min(target, deadline):
            if last_motors is not None:
                plant.apply_motors(last_motors)
            if plant.step() == -1:
                return False
            n_steps[0] += 1
        return True

    # Complementary/baro ASL is absolute (~20 m at P0). For --fw-zdist, command
    # ASL = ground_asl + target so Absolute-Z matches the baro estimate frame.
    asl_ground: list[float | None] = [None]
    z_cmd_asl: list[float] = [args.alt_target]
    alt_i = [0.0]

    def _refresh_z_cmd(pressure_pa: float) -> float:
        if asl_ground[0] is None:
            asl_ground[0] = pressure_pa_to_cf_asl(pressure_pa)
            z_cmd_asl[0] = asl_ground[0] + args.alt_target
            print(
                f"FW zdist ASL ground={asl_ground[0]:.2f}m "
                f"-> setpoint.z={z_cmd_asl[0]:.2f}m (rel +{args.alt_target:.2f})",
                flush=True,
            )
        return z_cmd_asl[0]

    def _inject_closed_loop(thrust: float, pressure_pa: float = 101325.0) -> None:
        if fw_zdist:
            z = _refresh_z_cmd(pressure_pa)
            renode.inject_setpoint_zdist(
                args.roll, args.pitch, args.yaw_rate, z
            )
        else:
            renode.inject_setpoint_rpyt(
                args.roll, args.pitch, args.yaw_rate, thrust
            )

    def _host_alt_thrust(alt: float, vz: float, dt: float, prev_thrust: float) -> tuple[float, float]:
        """Commander PD+I on plant altitude -> RPYT thrust. Returns (thrust, u)."""
        err = _clamp(args.alt_target - alt, -1.5, 1.5)
        vz_c = _clamp(vz, -2.5, 2.5)
        # Freeze integrator while climbing through large positive error after takeoff.
        if alt > 0.15 or abs(err) < 0.8:
            alt_i[0] = _clamp(alt_i[0] + err * dt, -1.0, 1.0)
        u = args.alt_kp * err - args.alt_kd * vz_c + args.alt_ki * alt_i[0]
        bias = args.hover_thrust
        if alt < 0.12:
            bias = max(bias, args.hover_thrust + 4000.0)
        thrust = _clamp(bias + u * 14000.0, 16000.0, 52000.0)
        return thrust, u

    try:
        if use_webots:
            for _ in range(5):
                plant.step()

        renode.feed_imu_burst(10, 10, 20, 30, 0, 0, 1000)
        renode.set_imu_sample(0.0, 0.0, 0.0, 0.0, 0.0, 1000.0, 101325.0, 25.0)
        print("IMU stream started (hover rest for calib).", flush=True)

        last_motors = None
        if args.wait_calib or args.wait_preflight:
            print(
                "Waiting for firmware gates (Webots paused — avoid floor-contact lock)...",
                flush=True,
            )
            gate_t0 = time.time()
            last_gate_print = 0.0
            while True:
                now = time.time()
                if (now - last_gate_print) >= 2.0:
                    renode.set_imu_sample(0.0, 0.0, 0.0, 0.0, 0.0, 1000.0, 101325.0, 25.0)
                    ok_c = renode.calib_ok()
                    ok_p = renode.preflight_ok()
                    st = (renode.client.query("imu_stream_status") or "").strip().replace("\n", " ")
                    print(f"  calib={int(ok_c)} preflight={int(ok_p)} {st}", flush=True)
                    last_gate_print = now
                    if args.wait_preflight and ok_p:
                        break
                    if args.wait_calib and not args.wait_preflight and ok_c:
                        break
                if (now - gate_t0) > max(args.seconds, args.gate_timeout):
                    print("TIMEOUT waiting for gates", file=sys.stderr)
                    return 2
                time.sleep(0.05)

        if args.mode == "motors-open-loop":
            print(
                f"Open-loop base_ratio={base_ratio:.3f} (hover_map={hover_r:.3f})"
                + (f" alt_target={args.alt_target:.2f}m" if hold_alt_ol else ""),
                flush=True,
            )
            r0 = MotorRatios(base_ratio, base_ratio, base_ratio, base_ratio)
            renode.force_motors(r0)
            plant.apply_motors(r0)
            last_motors = r0
        elif args.mode == "closed-loop":
            renode.clear_motor_force()
            if fw_zdist:
                print(
                    f"Closed-loop FW zdist rel_z={args.alt_target:.2f}m "
                    f"(Absolute-Z; motors 1:1 from FW mixer)",
                    flush=True,
                )
            elif host_alt:
                print(
                    f"Closed-loop alt-hold z={args.alt_target:.2f}m "
                    f"hover_T={args.hover_thrust:.0f} "
                    f"(host thrust PD; FW attitude mixer -> props 1:1)",
                    flush=True,
                )
            else:
                print(
                    f"Closed-loop RPYT fixed thrust={args.thrust:.0f}",
                    flush=True,
                )
            sample0 = plant.read_sensors()
            t_init = args.hover_thrust if host_alt else args.thrust
            _inject_closed_loop(t_init, float(sample0.pressure))

        t0 = time.time()
        sim0 = _sim_time()
        last_status = t0
        last_control = 0.0
        last_imu = 0.0
        last_thrust = args.hover_thrust if host_alt else args.thrust
        n_box = [0]
        u = 0.0
        if args.telemetry:
            telem = TelemetryWriter(args.telemetry)
            print(f"Telemetry JSONL -> {args.telemetry}", flush=True)
            telem.write_raw(
                "session_start",
                mode=args.mode,
                alt_hold=host_alt,
                fw_zdist=fw_zdist,
                alt_target=args.alt_target,
            )
        sample = plant.read_sensors()
        while True:
            loop_t0 = time.time()
            if use_webots and not _catch_up(t0, sim0, n_box):
                print("Webots quit.", flush=True)
                break

            now = time.time()
            sample = plant.read_sensors()
            alt = float(getattr(sample, "altitude_m", 0.0))
            dt = max(1e-3, now - prev_t)
            vz = (alt - prev_alt) / dt
            prev_alt, prev_t = alt, now

            if (now - last_imu) >= imu_period:
                _push_sensors(renode, sample, zero_gyro=False)
                last_imu = now

            if (now - last_control) >= period:
                if args.mode == "sensors-only":
                    u = 0.0
                elif args.mode == "motors-open-loop":
                    if hold_alt_ol:
                        alt_err = _clamp(args.alt_target - alt, -1.0, 1.0)
                        vz_c = _clamp(vz, -2.0, 2.0)
                        u = args.alt_kp * alt_err - args.alt_kd * vz_c
                    else:
                        u = 0.0
                    ratio = _clamp(base_ratio + u, 0.05, 0.9)
                    force = MotorRatios(ratio, ratio, ratio, ratio)
                    renode.force_motors(force)
                    if use_webots and hasattr(plant, "unstick_for_takeoff") and ratio > 0.4:
                        plant.unstick_for_takeoff()
                    plant.apply_motors(force)
                    last_motors = force
                else:
                    if fw_zdist:
                        thrust, u = last_thrust, 0.0
                    elif host_alt:
                        thrust, u = _host_alt_thrust(alt, vz, dt, last_thrust)
                    else:
                        thrust, u = args.thrust, 0.0
                    _inject_closed_loop(thrust, float(sample.pressure))
                    last_thrust = thrust
                    motors = renode.read_motors()
                    if motors is not None:
                        if use_webots and hasattr(plant, "maybe_allow_reunstick"):
                            plant.maybe_allow_reunstick(alt)
                        if (
                            use_webots
                            and hasattr(plant, "unstick_for_takeoff")
                            and motors.mean() > 0.35
                            and alt < 0.12
                        ):
                            plant.unstick_for_takeoff()
                        plant.apply_motors(motors)
                        last_motors = motors
                    if telem is not None:
                        omegas: list[float] = [0.0, 0.0, 0.0, 0.0]
                        if use_webots and hasattr(plant, "motor_velocities"):
                            omegas = list(plant.motor_velocities())
                        mvals = (
                            [last_motors.m1, last_motors.m2, last_motors.m3, last_motors.m4]
                            if last_motors
                            else [0.0, 0.0, 0.0, 0.0]
                        )
                        telem.write_cycle(
                            CycleSample(
                                t_wall=now,
                                sim=_sim_time(),
                                alt=alt,
                                vz=vz,
                                thrust=last_thrust,
                                u=u,
                                motors=mvals,
                                omega=omegas,
                                gyro=[sample.gx, sample.gy, sample.gz],
                                accel=[sample.ax, sample.ay, sample.az],
                                pressure=float(sample.pressure),
                            )
                        )
                last_control = now

            if not use_webots:
                plant.step()
                n_box[0] += 1

            n = n_box[0]
            if now - last_status >= args.status_every:
                motors = last_motors
                mtxt = (
                    f"m=({motors.m1:.3f},{motors.m2:.3f},{motors.m3:.3f},{motors.m4:.3f})"
                    if motors
                    else "m=(?)"
                )
                wv = ""
                if use_webots and hasattr(plant, "motor_velocities"):
                    v = plant.motor_velocities()
                    wv = f" ω=({v[0]:.0f},{v[1]:.0f},{v[2]:.0f},{v[3]:.0f})"
                sp = ""
                try:
                    sp_raw = (renode.setpoint_stats() or "").strip().replace("\n", " ")
                    if sp_raw:
                        sp = f" sp=[{sp_raw}]"
                except Exception:
                    pass
                print(
                    f"{now:.3f} n={n} sim={_sim_time():.2f} alt={alt:.3f} vz={vz:.2f} "
                    f"u={u:+.3f} T={last_thrust:.0f} {mtxt}{wv} "
                    f"g=({sample.gx:.1f},{sample.gy:.1f},{sample.gz:.1f}) "
                    f"a=({sample.ax:.0f},{sample.ay:.0f},{sample.az:.0f}) "
                    f"P={sample.pressure:.0f}{sp}",
                    flush=True,
                )
                last_status = now

            if args.seconds > 0 and (now - t0) >= args.seconds:
                print(f"Done after {args.seconds:.0f}s. final_alt={alt:.3f}m", flush=True)
                break

            if not use_webots:
                slept = time.time() - loop_t0
                if slept < period:
                    time.sleep(period - slept)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            if args.mode == "closed-loop":
                renode.clear_setpoint()
            renode.clear_motor_force()
        except Exception:
            pass
        if telem is not None:
            try:
                telem.write_raw("session_end", n_cycles=telem.count)
                telem.close()
            except Exception:
                pass
        renode.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
