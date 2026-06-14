<p align="center">
  <img src="docs/logo.png" alt="Pharos logo" width="220">
</p>

<h1 align="center">Pharos</h1>

<p align="center">
  A web-based ground control station for PX4 drones — live telemetry, manual
  flight, mission planning, and geofencing from a browser.
</p>

<p align="center">
  <img src="docs/screenshot-dashboard.png" alt="Pharos dashboard screenshot" width="100%">
</p>

<p align="center"><em>Live dashboard: dark map with a tracked drone position, telemetry readout, flight commands, manual control joysticks, mission planning, and geofencing — all in one view.</em></p>

---

## Features

**Live telemetry**
- Real-time position, altitude, and connection status streamed over a WebSocket
- Dark-themed live map (Leaflet) with a following drone marker and flight trail

**Flight commands**
- Arm, Takeoff, Land, and Return to Launch from the dashboard

**Manual flight control**
- On-screen joysticks (move + vertical/yaw) and keyboard control (W/A/S/D, Q/E, R/F)
- Implemented via MAVSDK offboard velocity setpoints, streamed continuously to PX4
- Move joystick is map-relative (north/east), automatically rotated into the
  drone's body frame using its live compass heading

**Autonomous mission planning**
- Click waypoints directly on the map to build a route
- Upload and run the mission on PX4, with live "Waypoint X / Y" progress
  reported back to the dashboard

**Geofencing**
- Draw an inclusion-boundary polygon on the map
- Upload it to PX4 as an enforced geofence (breach action: Hold)
- Clear the fence from the vehicle at any time

---

## Architecture

```
PX4 SITL (firmware)
      │  MAVLink / UDP (14540)
      ▼
MAVSDK-Python  ──  shared connection
      │
      ▼
FastAPI backend (server.py)
      │  WebSocket (telemetry/state)  +  REST (commands)
      ▼
Browser frontend (Leaflet + vanilla JS)
```

The backend maintains a MAVSDK connection per drone (3 by default, ports
14540/14541/14542) and runs five background tasks per drone (telemetry,
mission progress, heading, ground speed, battery), broadcasting each drone's
state — tagged with a `drone_id` — to all connected browser clients over a
single WebSocket. Flight commands, mission uploads, geofence uploads, and
manual control setpoints are sent as REST calls to `/api/<drone_id>/...`,
translating directly into MAVSDK action/mission/offboard/geofence calls.
`/api/all/...` endpoints fan a command out to every connected drone. The
frontend shows every drone on the map; single-drone panels (telemetry,
charts, manual control, mission planning, geofence) operate on whichever
drone is currently selected in the Drones panel.

---

## Tech stack

- **Backend:** Python, [FastAPI](https://fastapi.tiangolo.com/), [MAVSDK-Python](https://mavsdk.mavlink.io/)
- **Realtime transport:** WebSocket (telemetry) + REST (commands)
- **Frontend:** Vanilla JavaScript, [Leaflet](https://leafletjs.com/) (dark CARTO tiles)
- **Simulation:** [PX4 SITL](https://docs.px4.io/main/en/simulation/) (SIH backend)

---

## Getting started

Targets **PX4 SITL** (simulation) — no real hardware required. Developed and
tested on **WSL2 / Ubuntu**.

### 1. Backend

```bash
cd ~/pharos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. PX4 SITL

Clone and build [PX4-Autopilot](https://github.com/PX4/PX4-Autopilot) following
the [official setup guide](https://docs.px4.io/main/en/dev_setup/dev_env_linux_ubuntu.html).
Then run the headless SIH simulator (no Gazebo/Java required), set to start at
TED University, Ankara — matching the dashboard's default map view:

```bash
cd ~/PX4-Autopilot
PX4_HOME_LAT=39.9228214 PX4_HOME_LON=32.8618589 PX4_HOME_ALT=850 \
  make px4_sitl sihsim_quadx
```

PX4 SITL streams MAVLink over UDP to `localhost:14540`, which the backend
connects to automatically.

### 3. Run Pharos

With PX4 SITL running, in a second terminal:

```bash
cd ~/pharos/backend
source ../.venv/bin/activate
uvicorn server:app --reload --port 8000
```

Open **http://localhost:8000** in your browser. Once PX4 reports a connection,
the dashboard will go live: telemetry starts updating, and flight/mission/
geofence/manual controls become available.

---

## Status / roadmap

Pharos is built incrementally as a learning and portfolio project. Possible
next steps:

- Flight-log analysis and replay
- ~~Multi-drone / swarm support~~ (done — see `scripts/start_swarm.sh`)
- ~~Telemetry charts (altitude, speed, battery over time)~~ (done)
