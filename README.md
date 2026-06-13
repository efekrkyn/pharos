# drone-gcs — Sprint 0

A minimal Python script that connects to a PX4 SITL (Software In The Loop)
instance via MAVSDK and prints live position telemetry. This is the
foundation for a larger web-based Ground Control Station built in later
sprints.

## Repo layout

```
drone-gcs/
├── README.md
├── requirements.txt
├── backend/
│   ├── main.py
│   └── drone/
│       ├── __init__.py
│       └── connection.py
└── .gitignore
```

- `backend/drone/connection.py` — connects to the drone over MAVLink/UDP and
  waits until MAVSDK reports the system as connected.
- `backend/main.py` — connects, subscribes to position telemetry, and prints
  latitude/longitude/altitude as it updates.

---

## 1. System setup (inside WSL Ubuntu)

All commands below run **inside your WSL Ubuntu shell**, not Windows/PowerShell.

### 1.1 Base packages

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip build-essential
```

### 1.2 Python virtual environment for this project

```bash
cd ~/drone-gcs
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

You should now have `mavsdk` installed in `.venv`. Verify:

```bash
python -c "import mavsdk; print(mavsdk.__version__)"
```

> **Note on Python 3.14**: MAVSDK-Python 3.15.x ships a pure-Python wheel
> (`mavsdk-3.15.3-py3-none-...`) plus a precompiled `mavsdk_server` backend
> binary. It installs and imports cleanly on Python 3.14 — no fallback to an
> older Python is needed for this project.

---

## 2. Building and running PX4 SITL

PX4 SITL is a separate project (not part of this repo). Clone and build it
**outside** `~/drone-gcs`, e.g. directly in your home directory.

These steps follow the official PX4 guide — if anything below drifts from
what you see on screen, defer to the official docs:
https://docs.px4.io/main/en/dev_setup/dev_env_linux_ubuntu.html and
https://docs.px4.io/main/en/simulation/

### 2.1 Clone PX4

```bash
cd ~
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
cd PX4-Autopilot
```

### 2.2 Install PX4's build toolchain

PX4 provides a script that installs everything needed for SITL builds
(simulation tools, gcc, Python build deps, etc.). Run it once:

```bash
bash ./Tools/setup/ubuntu.sh
```

This may take a while and can prompt for your password (it uses `sudo`).
**After it finishes, close and reopen your WSL terminal** (or run
`exec bash`) so updated group memberships and environment variables take
effect.

### 2.3 Build and launch SITL with a multicopter target

From `~/PX4-Autopilot`, build and run the standard quadrotor SITL target
(uses the jMAVSim simulator, which is lighter than Gazebo and sufficient for
telemetry testing):

```bash
make px4_sitl jmavsim
```

The first build takes several minutes. When it finishes, PX4 SITL starts
automatically and you'll see the `pxh>` PX4 shell prompt along with a
jMAVSim simulator window (if you have an X server set up on Windows; if not,
the simulation still runs headless and MAVLink is still available — the GUI
window just won't display).

**Leave this terminal running.** This is "Terminal 1".

---

## 3. The two-terminal workflow

### Terminal 1 — PX4 SITL

```bash
cd ~/PX4-Autopilot
make px4_sitl jmavsim
```

Wait until you see the `pxh>` prompt. By default, PX4 SITL sends MAVLink
telemetry over UDP to `localhost:14540` — this is the port our script
listens on (`udp://:14540`).

### Terminal 2 — this project's script

```bash
cd ~/drone-gcs
source .venv/bin/activate
python backend/main.py
```

### What you should see

In Terminal 2, after `connection.py` logs that a system was discovered, you
should see continuously updating lines like:

```
lat=47.3977415 lon=8.5455932 abs_alt=488.12m rel_alt=0.01m
```

Press `Ctrl+C` in Terminal 2 to stop the script cleanly.

---

## 4. Verifying it works

1. Start PX4 SITL in Terminal 1 and wait for the `pxh>` prompt.
2. In Terminal 2, run `python backend/main.py`.
3. Confirm:
   - The log line `Connecting to drone at udp://:14540 ...` appears.
   - Within a few seconds, `Drone connected (system discovered)` appears.
   - Latitude/longitude/altitude values start printing and update over time.
4. If PX4 SITL is **not** running, the script should log a clear error after
   ~10 seconds (the connection timeout) and exit instead of hanging forever.

If you don't see a connection within the timeout while PX4 SITL is running,
check:

- PX4 SITL's console output for the MAVLink UDP port it's broadcasting to
  (it should include `14540`).
- That nothing else is bound to UDP port 14540 (`ss -ulnp | grep 14540`).
- That you're running both terminals inside the same WSL instance (not one
  in WSL and one in Windows).

---

## Next: Sprint 1

Sprint 1 will wrap this connection logic in a FastAPI app with a WebSocket
endpoint, so telemetry can be streamed to a browser-based frontend instead of
the console.
