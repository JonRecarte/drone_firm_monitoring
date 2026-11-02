# Bridge package (`bridge/`)

Host process that connects **Webots** (or `FakePlant`) to **Renode CF2.1**
(`cf2.elf` via monitor `:9999`).

Architecture: [../PURE_FW_LOOP.md](../PURE_FW_LOOP.md) · probes: [../ORCHESTRATION.md](../ORCHESTRATION.md)

## Modules

| File | Role |
|------|------|
| `webots_renode_bridge.py` | Main loop: setpoints, motors, IMU, altitude PD |
| `webots_io.py` | `WebotsPlant` / `FakePlant` — props + sensors |
| `renode_io.py` | Monitor client wrapper |
| `frames.py` | Gyro/accel units, ASL helper, axis map, LPF |
| `setpoint.py` | Pack RPYT / zDistance blobs (tests) |
| `motor_map.py` | Ratio helpers / FakePlant thrust map |
| `telemetry.py` | Optional JSONL probe stream |

## Closed-loop (default demo)

```bash
# Prefer the wrapper (starts Renode + Webots as needed)
FAKE=0 ALT=0.5 ./scripts/run_hover_demo.sh

# Or bridge alone (Renode already up, Webots world open with <extern>)
python3 -m bridge.webots_renode_bridge --mode closed-loop --alt-hold \
  --alt-target 0.5 --wait-preflight --imu-hz 80 --loop-hz 12 --seconds 45 \
  --telemetry /tmp/cf_bridge.jsonl
```

| Flag | Meaning |
|------|---------|
| `--alt-hold` | Host PD on plant altitude → thrust (default on) |
| `--no-alt-hold` | Fixed `--thrust` |
| `--fw-zdist` | Experimental FW Absolute-Z setpoint |
| `--telemetry PATH` | Append JSONL cycle samples |
| `--gyro-sign sx,sy,sz` | Axis sign remap |
| `--fake` | FakePlant (no Webots) |

## Modes

| Mode | Behavior |
|------|----------|
| `sensors-only` | Plant IMU → Renode; motors ignored |
| `motors-open-loop` | Force equal ratios into FW + plant |
| `closed-loop` | RPYT setpoint; FW mixer → props 1:1; IMU back |

## Monitor helpers (bridge not running)

```bash
./scripts/monitor_motors.sh 9999 --once
./scripts/check_setpoint.sh 9999 35000
./scripts/monitor_cpu.sh 9999          # exclusive -P — not with bridge
```
