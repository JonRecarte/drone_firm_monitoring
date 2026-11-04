# Where the code comes from

`drone_firm_monitoring` includes copies of two projects. This page lists where each part
comes from and what was changed, so it can be compared with the original.

| Folder | Origin | Version copied |
|---|---|---|
| `engine/bridge/`, `engine/scripts/renode_monitor.py`, `engine/scripts/monitor_cf.py` | [CF_renode_emulator](https://github.com/danielalaez/CF_renode_emulator) (Dani, `danielalaez`) | commit `fdb018b` (2026-10-23) |
| `examples/protos/` (`Crazyflie.proto` and `meshes/`) | Same repository; official Cyberbotics/Bitcraze model, "licensed for use only with Webots" | commit `fdb018b` |
| `examples/worlds/` | Based on `webots/worlds/cf_hover.wbt` from the same repository | commit `fdb018b` |
| `monitoring/` | [drone-monitoring](https://github.com/JonRecarte/drone-monitoring) (Jon, `JonRecarte`) | commit `e8dcd06` |
| `monitoring/tests/data/cf_bridge_smoke_jsonl_example.txt` | Example flight log by Dani (2026-10-23) | — |

## Changes from the original

In `engine/` (compared with `CF_renode_emulator@fdb018b`):

- `scripts/monitor_cf.py`: RAM addresses updated to `crazyflie-firmware@b48aba18`. At run
  time they are replaced by those in the firmware profile (`scripts/firmware_profile.py`).
  Struct offsets are unchanged (checked with `gdb ptype /o`).
- `bridge/webots_renode_bridge.py`:
  - `--gate-timeout` option (how long to wait for the firmware to boot; default 180 s, as
    before).
  - The altitude PD+I runs in simulated time (Webots `robot.getTime()`) instead of wall
    time, so its gains no longer depend on how fast the machine is (agreed with Dani,
    2026-11-02). The gains were converted so the drone behaves as before on the machine
    where the original ones were tuned (0.31 simulated seconds per wall second): `kd`
    1.10 → 0.341, `ki` 0.04 → 0.129, integrator limit 1 → 0.31 m·s, vz clamp
    2.5 → 8.06 m/s. New options `--alt-i-limit` and `--alt-vz-limit`. The first cycle no
    longer takes the boot wait as its `dt`, which used to start the integrator saturated.
    With FakePlant (no Webots clock) the controller still uses wall time, but with the new
    gains, so FakePlant flights are not comparable with older ones. `dfm` does not use
    FakePlant.
- `bridge/webots_io.py`: `unstick_for_takeoff` keeps the drone's own `x, y`. It used to move
  it to `(0, 0)`, which piled up several drones in the same spot.

In `monitoring/` (compared with `drone-monitoring@e8dcd06`):

- Everything translated to English: messages and help of `bridge_to_influx.py` (the
  summary line now reads `lines=… cycles=… invalid=… non_finite_values=…`), comments,
  and the dashboard's titles, descriptions and column names. The code, the InfluxDB
  schema and the dashboard layout are unchanged.
- `tests/test_bridge_to_influx.py`: the example log is read from `tests/data/`.
- `grafana/build_dashboard.py`: **Drone** selector (`drone` tag), one line per drone in
  every chart and one value per drone in the summary; points without the tag show up as
  `(untagged)`. The **Session** selector was removed. Time-series panels that used three
  fixed decimals now use automatic ones, so an axis reads `12.5 m` instead of `12.500 m`.

## Not copied

- `dfm`, `config.example.toml`, `image/` (`Dockerfile` and `entrypoint.py`) and
  `engine/scripts/firmware_profile.py` are new in this project.
- The image builds, unmodified and at the versions pinned in `image/Dockerfile`:
  [crazyflie-firmware](https://github.com/bitcraze/crazyflie-firmware) `2026.08`
  (`54f31e24`; `cf2.elf` sha256 `7c27a4cc…`),
  [Bitcraze's Renode](https://github.com/bitcraze/renode) `975de43`,
  `renode-infrastructure` `c9c51b2d` and `renode-resources` `7b7d316`.
- The `.resc` in `image/entrypoint.py` is the one `scripts/run_first_test.sh` generated in
  CF_renode_emulator.

## Licence

This repository is MIT-licensed (`LICENSE`). The image also contains the Crazyflie
firmware (GPL-3.0) and Renode (MIT); see the third-party section of `LICENSE`.
