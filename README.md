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
- [Webots R2025a](https://cyberbotics.com) on the same machine. On Ubuntu, download
  `webots_2025a_amd64.deb` from the Cyberbotics website and, in the folder where you
  downloaded it, run `sudo apt install ./webots_2025a_amd64.deb`. The first time Webots
  starts it opens a demo world; you can close it. If you installed Webots from the
  tarball instead, you may also need `sudo apt install libsndio7.0`.

Each drone needs about one CPU core and 250 MB of RAM.

## Install

```bash
git clone https://github.com/JonRecarte/drone_firm_monitoring.git
cd drone_firm_monitoring
./dfm setup     # checks requirements, downloads the images, starts Grafana, creates the dfm command
./dfm check     # tests the installation without Webots (takes a few minutes)
```

`dfm setup` creates the `dfm` command in `~/.local/bin`. If it says that this folder is
not on your `PATH`, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` and open a
new terminal. From then on you can type `dfm` from any folder; until then, use `./dfm`
inside this one.

## First flight

Try the ready-made example before using your own world:

1. In Webots, choose **File → Open World…** and open `examples/worlds/one_drone.wbt`
   inside the folder you cloned, for example
   `~/drone_firm_monitoring/examples/worlds/one_drone.wbt`.
2. Press **▶**. The drones are only visible to `dfm` while the simulation is running.
3. In a terminal, run:

   ```bash
   dfm up
   ```

`dfm` finds every drone waiting for an external controller and starts a Renode container,
a bridge and a telemetry ingester for each one. A status table shows what each drone is
doing. On our test machine (6 cores) this is what happens:

- **For about 2 minutes** the firmware boots and calibrates. Meanwhile the Webots clock
  stands still (`0.00x`): this is on purpose, Webots has not frozen.
- Then the drone takes off, rises to about 0.8 m and settles at about 0.6 m some 6 s
  later (simulated time). The firmware keeps it level.
- **The emulation is slow**: Webots shows about `0.02x`, so the default flight of 30 s
  (simulated) takes **about 20 minutes**. See [Known limitations](#known-limitations).
- When the first drone finishes its flight (`--duration`), all of them stop. Press
  `Ctrl+C` to stop earlier.

If Webots asks whether to save the example world, answer **No**, so the example stays as
it is. `examples/worlds/two_drones.wbt` does the same with two drones.

Each run is saved in `runs/<date-time>/`: one telemetry file (`.jsonl`) and logs per drone,
plus the configuration and versions used.

## Watch in Grafana

Open <http://localhost:3000> (user `admin`, password `admin`) and the dashboard
**Crazyflie · Webots↔Renode bridge**. Use the **Drone** selector to show one, several or
all drones.

Flights of drones with the same name are drawn on the same line. To look at one flight
only, choose its time range in the top right corner, or drag across a chart to zoom in.

## Connect your own world

Keep your worlds **outside** the `drone_firm_monitoring` folder, so that updating or
removing `dfm` never touches them.

In Webots, for every drone that should run the emulated firmware:

1. Use the `Crazyflie` robot **from `examples/protos/`**: copy that folder (with
   `meshes/`) into your project's `protos/` folder and add it with **Add → PROTO nodes
   (Current Project) → Crazyflie**. Do not use the one in the Webots library: its rotors
   react too slowly and its yaw torque is ten times too strong, so the firmware cannot
   keep it level.
2. Give it a **unique `name`**, for example `cf1`.
3. Set `controller` to **`"<extern>"`**.
4. Set `supervisor` to **`TRUE`** (the bridge uses the supervisor API).

In `WorldInfo`, set **`basicTimeStep` to `2`** (ms). The firmware runs its control loop
every 1–2 ms, and with larger steps the drone starts to shake. If your world uses a larger
step or the library Crazyflie, `dfm up` shows a warning under its table.

Save the world, press **▶** and run `dfm up`, as in the first flight. Drones with a normal
Webots controller are left alone, so emulated and regular drones can share a world.

## Commands

| Command | What it does |
|---|---|
| `dfm setup` | Checks requirements, downloads the images, starts InfluxDB/Grafana and creates the `dfm` command in `~/.local/bin`. Run it once. |
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
  Webots and the firmware advance **in turns of one physics step** (lockstep): Webots
  simulates 2 ms with the current motor outputs; the bridge passes the drone's gyro,
  accelerometer and barometer, plus a roll/pitch/yaw-rate/thrust setpoint, to the
  firmware; Renode runs exactly 2 ms of firmware; and the new motor outputs go back to the
  propellers. The firmware stabilises attitude and mixes the motors; the bridge holds
  altitude.
- Each Renode starts paused and only runs when the bridge advances it, so the result does
  not depend on how fast the machine is: the same world and settings give the same flight.
- The firmware's memory addresses are read from its debug information when the image is
  built (`engine/scripts/firmware_profile.py`). A profile that does not match the exact
  binary is refused.

## Known limitations

- **Speed.** On our test machine the emulation runs at about 1/40 of real time (30 s of
  flight take about 20 minutes). Most of that time is spent in Renode's model of the I2C
  bus that the firmware uses to read its sensors: it raises hundreds of spurious
  interrupts on every read. The firmware itself is unchanged. Drones run in parallel, one
  container each.
- **Drone model.** The Crazyflie in `examples/protos/` has the real yaw torque and faster
  rotors, but its mass and inertia are still about twice those of the real Crazyflie.
- **Altitude.** The bridge, not the firmware, holds the altitude. With the default gains
  the drone settles about 0.1 m above the target (0.60 m for 0.5 m).
- Emulation is functionally faithful (memory, peripherals, boot, arming), not
  cycle-accurate.
- Version 0.1 (branch `original`) keeps the emulator as it was in CF_renode_emulator, as a
  reference: there, the firmware never received the flight sensor data.

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
