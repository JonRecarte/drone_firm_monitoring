# drone_firm_monitoring

Run the **real Crazyflie firmware** for the drones in your **Webots** world. Each drone
you mark as external gets its own emulated STM32 microcontroller (Renode) running the
actual `cf2.elf`, a bridge that connects it to its drone in Webots, and live telemetry
in Grafana. Works with any number of drones.

You build the world in Webots as usual; `dfm` only adds the firmware emulation. It never
opens or closes Webots.

```
              Webots (your world, N drones)
        ┌───────────┬───────────┬───────────┐
        │    cf1    │    cf2    │    cf3    │
        └─────▲─────┴─────▲─────┴─────▲─────┘
           bridge      bridge      bridge       one per drone (Python, on your machine)
           Renode      Renode      Renode       one Docker container per drone (real firmware)
              └───────────┴───────────┴──► InfluxDB + Grafana
```

## Requirements

You need these before installing (`dfm` checks them, but does not install them):

- Linux x86_64.
- [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin, usable
  **without sudo**. Run `sudo usermod -aG docker $USER`, then log out and back in.
- Python 3.11 or newer.
- [Webots R2025a](https://cyberbotics.com), installed and working on the same machine.
  If you installed it from the tarball, you may also need `sudo apt install libsndio7.0`.

Each drone needs about one CPU core and 250 MB of RAM.

## Install

```bash
git clone https://github.com/JonRecarte/drone_firm_monitoring.git
cd drone_firm_monitoring
./dfm setup     # checks requirements, downloads the images, starts Grafana, puts dfm on your PATH
dfm check       # tests the installation without Webots (takes a few minutes)
```

If `dfm setup` says that `~/.local/bin` is not on your `PATH`, add
`export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` and open a new terminal. Until
then, use `./dfm`.

## Connect your world

In Webots, for every drone that should run the emulated firmware:

1. Use the `Crazyflie` robot. Any robot with the same devices also works: `gps`, `gyro`
   and `m1_motor`…`m4_motor` (plus `inertial_unit`, which is used if present).
2. Give it a **unique `name`**, for example `cf1`.
3. Set `controller` to **`"<extern>"`**.
4. Set `supervisor` to **`TRUE`**. The bridge needs it to lift the drone off the floor at
   take-off.

Drones with a normal Webots controller are left alone, so emulated and regular drones can
share a world. Two ready-made worlds are in `examples/worlds/` (`one_drone.wbt`,
`two_drones.wbt`).

## Fly

1. Open your world in Webots and press **▶**. The drones are only visible to `dfm` while
   the simulation is running.
2. Run:

   ```bash
   dfm up
   ```

`dfm` finds every drone waiting for an external controller and starts a Renode container,
a bridge and a telemetry ingester for each one. A status table shows what each drone is
doing:

- The firmware takes **2–4 minutes** to boot and calibrate in emulation.
- Arming takes about one more minute. Then the drones take off and hold altitude.
- When the first drone finishes its flight (`--duration`, 300 s by default), all of them
  stop. Press `Ctrl+C` to stop earlier.

Each run is saved in `runs/<date-time>/`: one telemetry file (`.jsonl`) and logs per drone,
plus the configuration and versions used.

## Watch in Grafana

Open <http://localhost:3000> (user `admin`, password `admin`) and the dashboard
**Crazyflie · bridge Webots↔Renode**. Use the **Drone** selector to show one, several or
all drones.

## Commands

| Command | What it does |
|---|---|
| `dfm setup` | Checks requirements, downloads the images, starts InfluxDB/Grafana and puts `dfm` on your `PATH`. Run it once. |
| `dfm check` | Tests the installation without Webots: the firmware boots, calibrates, arms and drives the motors, and Grafana answers. Use `--quick` to only check that Renode starts. |
| `dfm up` | Connects to the running Webots and flies every external drone. Options: `--duration <s>`, `--port <port>` (if several Webots are open), `--no-monitoring`. |
| `dfm status` | Shows which Webots and drones `dfm` sees, and what is still running. |
| `dfm down` | Stops anything left over from a previous run. |
| `dfm uninstall` | Removes what `setup` installed. It asks before deleting saved flights (`--delete-data`); `--dry-run` shows what it would do. It never touches Webots, Docker or your worlds. |

## Configuration

Settings that Webots does not know about live in `config.toml`. Copy
`config.example.toml` to `config.toml` and edit the copy. Everything about the scene (how
many drones, where, their names) stays in your Webots world.

The main settings:

- `[bridge]`: altitude target, hover thrust, loop rates, flight duration, and how long to
  wait for the firmware to boot.
- `[renode]`: the image and the first port.
- `[monitoring]`: turn Grafana and the ingesters on or off.

## How it works

- **Renode** emulates the STM32F405 of a Crazyflie 2.1 and runs the unmodified firmware
  (`crazyflie-firmware` **2026.08**). Each drone gets its own container from the same
  image.
- The **bridge** is a Webots external controller (`WEBOTS_CONTROLLER_URL=ipc://<port>/<name>`).
  On every cycle it feeds the drone's gyro, accelerometer and barometer to the firmware,
  writes a roll/pitch/yaw-rate/thrust setpoint into the firmware's memory, and applies the
  four motor outputs to the propellers. The firmware stabilises attitude and mixes the
  motors; the bridge holds altitude.
- The firmware's memory addresses are read from its debug information when the image is
  built (`engine/scripts/firmware_profile.py`). A profile that does not match the exact
  binary is refused.

## Known limitations

- Webots, Renode and the bridges are not run in lockstep, and the bridge's altitude
  controller uses wall-clock time. Take-off can overshoot, and flights depend on how fast
  the machine is. A fix is being worked on.
- Emulation is functionally faithful (memory, peripherals, boot, arming), not
  cycle-accurate.

## Building the image yourself

`dfm setup` downloads the published image. To build it yourself from `image/Dockerfile`
(every version is pinned):

```bash
docker build -f image/Dockerfile -t drone_firm_monitoring:dev .
```

Then tell `dfm` to use it in `config.toml`:

```toml
[renode]
image = "drone_firm_monitoring:dev"
```

## Credits and licence

Based on [CF_renode_emulator](https://github.com/danielalaez/CF_renode_emulator) and
[drone-monitoring](https://github.com/JonRecarte/drone-monitoring). See `ORIGIN.md` for
what comes from where and what was changed.

This repository is released under the MIT licence (`LICENSE`). The Docker image also
contains the Crazyflie firmware (GPL-3.0; source at tag `2026.08`, built exactly as in
`image/Dockerfile`) and Renode (MIT).
