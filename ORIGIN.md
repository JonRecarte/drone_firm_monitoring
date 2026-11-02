# Where the code comes from

`drone_firm_monitoring` includes copies of two projects. This page lists where each part
comes from and what was changed, so it can be compared with the original.

| Folder | Origin | Version copied |
|---|---|---|
| `engine/bridge/`, `engine/scripts/renode_monitor.py`, `engine/scripts/monitor_cf.py` | [CF_renode_emulator](https://github.com/danielalaez/CF_renode_emulator) (Dani, `danielalaez`) | commit `fdb018b` (2026-10-23) |
| `examples/protos/` (`Crazyflie.proto` and `meshes/`) | Same repository; official Cyberbotics/Bitcraze model, "licensed for use only with Webots" | commit `fdb018b` |
| `examples/worlds/` | Based on `webots/worlds/cf_hover.wbt` from the same repository | commit `fdb018b` |
| `monitoring/` | [drone-monitoring](https://github.com/JonRecarte/drone-monitoring) (Jon, `JonRecarte`) | commit `e8dcd06` |
| `monitoring/tests/datos/cf_bridge_smoke_jsonl_example.txt` | Example flight log by Dani (2026-10-23) | — |

## Changes from the original

In `engine/` (compared with `CF_renode_emulator@fdb018b`):

- `scripts/monitor_cf.py`: RAM addresses updated to `crazyflie-firmware@b48aba18`. At run
  time they are replaced by those in the firmware profile (`scripts/firmware_profile.py`).
  Struct offsets are unchanged (checked with `gdb ptype /o`).
- `bridge/webots_renode_bridge.py`: `--gate-timeout` option (how long to wait for the
  firmware to boot; default 180 s, as before).
- `bridge/webots_io.py`: `unstick_for_takeoff` keeps the drone's own `x, y`. It used to move
  it to `(0, 0)`, which piled up several drones in the same spot.

In `monitoring/` (compared with `drone-monitoring@e8dcd06`):

- `tests/test_bridge_to_influx.py`: the example log is read from `tests/datos/`.
- `grafana/build_dashboard.py`: **Drone** selector (`drone` tag), one line per drone in
  every chart and one value per drone in the summary; the **Session** selector was
  removed.

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
