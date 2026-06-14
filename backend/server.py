"""FastAPI server that streams live PX4 telemetry to browser clients."""

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mavsdk import System
from mavsdk.action import ActionError
from mavsdk.geofence import FenceType, GeofenceData, GeofenceError, Point, Polygon
from mavsdk.mission import MissionError, MissionItem, MissionPlan
from mavsdk.offboard import OffboardError, VelocityBodyYawspeed
from mavsdk.param import ParamError
from pydantic import BaseModel

import llm_planner
from drone.connection import ConnectionTimeoutError, connect_drone
from telemetry_hub import TelemetryHub

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TAKEOFF_ALTITUDE_M = 5.0

# Cruise speed between waypoints and how close the vehicle must get to a
# waypoint before it's considered reached. Reasonable defaults for SITL.
MISSION_SPEED_M_S = 5.0
MISSION_ACCEPTANCE_RADIUS_M = 2.0

# PX4's GF_ACTION parameter: what the vehicle does on a geofence breach.
# 0=None, 1=Warning, 2=Hold, 3=Return, 4=Terminate, 5=Land. We set this to
# Hold whenever a geofence is uploaded so breaches are actually enforced
# and observable (the vehicle stops and hovers at the fence), and back to
# None when the geofence is cleared so a stale Hold can't keep forcing the
# vehicle out of OFFBOARD (e.g. during manual control) once there's no
# fence left to enforce.
GF_ACTION_HOLD = 2
GF_ACTION_NONE = 0

# Manual control: how fast we re-send velocity setpoints, and the speed
# limits applied to incoming manual command values.
MANUAL_SETPOINT_RATE_HZ = 10
MANUAL_MAX_HORIZONTAL_M_S = 8.0
MANUAL_MAX_VERTICAL_M_S = 4.0
MANUAL_MAX_YAW_RATE_RAD_S = 2.0

ActionFn = Callable[[System], Awaitable[None]]
hub = TelemetryHub()


@dataclass
class DroneState:
    """Per-drone connection and live state, one instance per swarm member."""

    drone_id: str
    label: str
    system_address: str
    mavsdk_server_port: int

    # Set once telemetry_task() connects; None while disconnected/reconnecting.
    drone: System | None = None

    # Current manual-control velocity setpoint, in MAVSDK's forward/right/down
    # body frame (down positive = descending). Updated by the manual/command
    # endpoint and streamed continuously to PX4 by _manual_setpoint_loop()
    # while active.
    manual_velocity: dict = field(
        default_factory=lambda: {"forward": 0.0, "right": 0.0, "down": 0.0, "yaw_speed_deg_s": 0.0}
    )

    # The background task streaming manual_velocity to PX4, or None if manual
    # control (offboard mode) isn't active.
    manual_task: asyncio.Task | None = None

    # Latest mission progress, merged into every telemetry broadcast so
    # clients get progress "for free". total == 0 means no mission is active.
    mission_state: dict = field(default_factory=lambda: {"mission_current": 0, "mission_total": 0})

    # Latest vehicle heading (compass heading, degrees clockwise from north),
    # merged into every telemetry broadcast. None until known.
    heading_state: dict = field(default_factory=lambda: {"heading_deg": None})

    # Latest horizontal ground speed (m/s), derived from velocity_ned() and
    # merged into every telemetry broadcast. None until known.
    velocity_state: dict = field(default_factory=lambda: {"ground_speed_m_s": None})

    # Latest battery state, merged into every telemetry broadcast. None until
    # known (and may stay None on SITL targets that don't simulate a battery).
    battery_state: dict = field(default_factory=lambda: {"battery_percent": None, "battery_voltage_v": None})


# The swarm: three drones connecting to PX4 SITL instances 0/1/2, which
# listen for MAVSDK on udp ports 14540/14541/14542 respectively (instance N
# uses port 14540+N). Keyed by drone_id, used throughout for routing
# telemetry and commands.
# Each drone's MAVSDK System() spawns its own mavsdk_server subprocess, which
# needs its own gRPC port (default 50051) — otherwise concurrent instances
# collide and MAVSDK silently talks to the wrong vehicle.
DRONES: dict[str, DroneState] = {
    "drone0": DroneState("drone0", "D0", "udp://:14540", 50051),
    "drone1": DroneState("drone1", "D1", "udp://:14541", 50052),
    "drone2": DroneState("drone2", "D2", "udp://:14542", 50053),
}


def _waiting_message(state: DroneState) -> dict:
    """The "disconnected/waiting" telemetry message for a drone."""
    return {
        "drone_id": state.drone_id,
        "label": state.label,
        "status": "waiting",
        "lat": None,
        "lon": None,
        "abs_alt": None,
        "rel_alt": None,
        **state.mission_state,
        **state.heading_state,
        **state.velocity_state,
        **state.battery_state,
    }


async def _broadcast_merged(state: DroneState) -> None:
    """Re-broadcast the latest known telemetry for `state`, with its sub-states refreshed.

    Used by the per-drone background tasks (mission progress, heading,
    velocity, battery) so each only needs to update its own slice of state
    and the rest of the last-known message is preserved.
    """
    base = hub.latest.get(state.drone_id) or _waiting_message(state)
    await hub.broadcast(
        {
            **base,
            **state.mission_state,
            **state.heading_state,
            **state.velocity_state,
            **state.battery_state,
        }
    )


async def telemetry_task(state: DroneState) -> None:
    """Maintain one drone's connection and broadcast its position updates.

    Runs for the lifetime of the app. If no drone is found, connect_drone()
    times out (10s) and we simply retry, leaving clients in "waiting" state
    for this drone. If a connected drone's telemetry stream ends, we go back
    to "waiting" and try to reconnect.
    """
    while True:
        try:
            state.drone = await connect_drone(state.system_address, port=state.mavsdk_server_port)
        except ConnectionTimeoutError as exc:
            logger.warning("[%s] %s", state.drone_id, exc)
            continue

        try:
            async for position in state.drone.telemetry.position():
                await hub.broadcast(
                    {
                        "drone_id": state.drone_id,
                        "label": state.label,
                        "status": "connected",
                        "lat": position.latitude_deg,
                        "lon": position.longitude_deg,
                        "abs_alt": position.absolute_altitude_m,
                        "rel_alt": position.relative_altitude_m,
                        **state.mission_state,
                        **state.heading_state,
                        **state.velocity_state,
                        **state.battery_state,
                    }
                )
        except Exception:
            logger.exception("[%s] Telemetry stream ended, will retry connection", state.drone_id)
            state.drone = None
            await _stop_manual_task(state)
            state.mission_state.update(mission_current=0, mission_total=0)
            state.heading_state.update(heading_deg=None)
            state.velocity_state.update(ground_speed_m_s=None)
            state.battery_state.update(battery_percent=None, battery_voltage_v=None)
            await hub.broadcast(_waiting_message(state))


async def mission_progress_task(state: DroneState) -> None:
    """Track mission_progress() for one drone and broadcast it.

    Merges into the same telemetry messages sent by telemetry_task() (via
    state.mission_state) so clients don't need a new message type. Retries
    quietly whenever there's no drone or the stream ends.
    """
    while True:
        current_drone = state.drone
        if current_drone is None:
            await asyncio.sleep(1)
            continue

        try:
            stream = current_drone.mission.mission_progress()
            while True:
                # If telemetry_task reconnected with a new drone/connection
                # while we were subscribed to the old one, this stream may
                # never yield again — bail out and resubscribe on the new one.
                if state.drone is not current_drone:
                    break
                try:
                    progress = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                state.mission_state.update(mission_current=progress.current, mission_total=progress.total)
                await _broadcast_merged(state)
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except Exception:
            logger.exception("[%s] Mission progress stream ended, will retry", state.drone_id)
            await asyncio.sleep(1)


async def heading_task(state: DroneState) -> None:
    """Track telemetry.heading() for one drone and broadcast it.

    Merges into the same telemetry messages sent by telemetry_task() (via
    state.heading_state), same reconnect-tolerant pattern as mission_progress_task().
    """
    while True:
        current_drone = state.drone
        if current_drone is None:
            await asyncio.sleep(1)
            continue

        try:
            stream = current_drone.telemetry.heading()
            while True:
                if state.drone is not current_drone:
                    break
                try:
                    heading = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                state.heading_state.update(heading_deg=heading.heading_deg)
                await _broadcast_merged(state)
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except Exception:
            logger.exception("[%s] Heading stream ended, will retry", state.drone_id)
            await asyncio.sleep(1)


async def velocity_task(state: DroneState) -> None:
    """Track telemetry.velocity_ned() for one drone and broadcast horizontal ground speed.

    Merges into the same telemetry messages sent by telemetry_task() (via
    state.velocity_state), same reconnect-tolerant pattern as heading_task().
    """
    while True:
        current_drone = state.drone
        if current_drone is None:
            await asyncio.sleep(1)
            continue

        try:
            stream = current_drone.telemetry.velocity_ned()
            while True:
                if state.drone is not current_drone:
                    break
                try:
                    velocity = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                ground_speed = math.hypot(velocity.north_m_s, velocity.east_m_s)
                state.velocity_state.update(ground_speed_m_s=ground_speed)
                await _broadcast_merged(state)
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except Exception:
            logger.exception("[%s] Velocity stream ended, will retry", state.drone_id)
            await asyncio.sleep(1)


async def battery_task(state: DroneState) -> None:
    """Track telemetry.battery() for one drone and broadcast remaining percent + voltage.

    Merges into the same telemetry messages sent by telemetry_task() (via
    state.battery_state), same reconnect-tolerant pattern as heading_task().
    Some SITL targets report NaN for battery fields; NaN is converted to
    None and the frontend treats it as "no data".
    """
    while True:
        current_drone = state.drone
        if current_drone is None:
            await asyncio.sleep(1)
            continue

        try:
            stream = current_drone.telemetry.battery()
            while True:
                if state.drone is not current_drone:
                    break
                try:
                    battery = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                percent = None if math.isnan(battery.remaining_percent) else battery.remaining_percent
                voltage = None if math.isnan(battery.voltage_v) else battery.voltage_v
                state.battery_state.update(battery_percent=percent, battery_voltage_v=voltage)
                await _broadcast_merged(state)
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except Exception:
            logger.exception("[%s] Battery stream ended, will retry", state.drone_id)
            await asyncio.sleep(1)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start each drone's background tasks on startup, cancel them on shutdown."""
    tasks: list[asyncio.Task] = []
    for state in DRONES.values():
        # Seed clients with a "waiting" snapshot for every drone before any
        # connection attempt completes, so the frontend knows about all of
        # them immediately even if some SITL instances never come up.
        hub.latest[state.drone_id] = _waiting_message(state)
        tasks += [
            asyncio.create_task(telemetry_task(state)),
            asyncio.create_task(mission_progress_task(state)),
            asyncio.create_task(heading_task(state)),
            asyncio.create_task(velocity_task(state)),
            asyncio.create_task(battery_task(state)),
        ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    """Serve the dashboard page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Register a client for telemetry broadcasts until it disconnects."""
    await hub.register(websocket)
    try:
        while True:
            # We don't expect messages from the client; this just blocks
            # until the connection is closed so we can clean up.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(websocket)


def _get_drone(drone_id: str) -> DroneState | JSONResponse:
    """Look up a drone by id, or a 404 JSONResponse if it doesn't exist."""
    state = DRONES.get(drone_id)
    if state is None:
        return JSONResponse({"ok": False, "error": f"Unknown drone '{drone_id}'"}, status_code=404)
    return state


async def _run_command(state: DroneState, action: ActionFn) -> JSONResponse:
    """Run a MAVSDK action coroutine against `state.drone` and translate the outcome to JSON.

    Returns 503 if no drone is connected, 400 if PX4 rejects the command
    (e.g. failed preflight checks for arm), and 200 on success.
    """
    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    try:
        await action(state.drone)
    except (ActionError, MissionError, OffboardError, GeofenceError, ParamError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return JSONResponse({"ok": True})


async def _fan_out(action: ActionFn) -> JSONResponse:
    """Run `action` against every connected drone and report per-drone results.

    Drones that aren't connected are reported as such rather than skipped
    silently. `ok` in the top-level response is true if at least one drone
    accepted the command.
    """
    results: dict[str, dict] = {}
    for drone_id, state in DRONES.items():
        if state.drone is None:
            results[drone_id] = {"ok": False, "error": "Drone not connected"}
            continue
        try:
            await action(state.drone)
        except (ActionError, MissionError, OffboardError, GeofenceError, ParamError) as exc:
            results[drone_id] = {"ok": False, "error": str(exc)}
            continue
        results[drone_id] = {"ok": True}

    overall_ok = any(result["ok"] for result in results.values())
    return JSONResponse({"ok": overall_ok, "results": results})


async def _takeoff_action(drone: System) -> None:
    """Arm (if needed) and take off to TAKEOFF_ALTITUDE_M.

    PX4 requires the vehicle to be armed before takeoff() is accepted, so we
    arm first. If it's already armed, arm() raises an ActionError that we
    ignore here.
    """
    with contextlib.suppress(ActionError):
        await drone.action.arm()
    await drone.action.set_takeoff_altitude(TAKEOFF_ALTITUDE_M)
    await drone.action.takeoff()


@app.post("/api/all/arm")
async def arm_all() -> JSONResponse:
    """Arm every connected drone in the swarm."""
    return await _fan_out(lambda d: d.action.arm())


@app.post("/api/all/takeoff")
async def takeoff_all() -> JSONResponse:
    """Arm (if needed) and take off every connected drone in the swarm."""
    return await _fan_out(_takeoff_action)


@app.post("/api/all/land")
async def land_all() -> JSONResponse:
    """Land every connected drone in the swarm at its current position."""
    return await _fan_out(lambda d: d.action.land())


@app.post("/api/all/rtl")
async def rtl_all() -> JSONResponse:
    """Return every connected drone in the swarm to its launch position."""
    return await _fan_out(lambda d: d.action.return_to_launch())


@app.post("/api/{drone_id}/arm")
async def arm(drone_id: str) -> JSONResponse:
    """Arm the vehicle. May fail if PX4 preflight checks haven't passed yet."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, lambda d: d.action.arm())


@app.post("/api/{drone_id}/takeoff")
async def takeoff(drone_id: str) -> JSONResponse:
    """Arm (if needed) and take off to TAKEOFF_ALTITUDE_M."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, _takeoff_action)


@app.post("/api/{drone_id}/land")
async def land(drone_id: str) -> JSONResponse:
    """Command the vehicle to land at its current position."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, lambda d: d.action.land())


@app.post("/api/{drone_id}/rtl")
async def rtl(drone_id: str) -> JSONResponse:
    """Command the vehicle to return to its launch position and land."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, lambda d: d.action.return_to_launch())


class Waypoint(BaseModel):
    lat: float
    lon: float


class MissionUploadRequest(BaseModel):
    altitude: float
    waypoints: list[Waypoint]


@app.post("/api/{drone_id}/mission/upload")
async def upload_mission(drone_id: str, request: MissionUploadRequest) -> JSONResponse:
    """Build a MissionPlan from the given waypoints and upload it to one drone."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if not request.waypoints:
        return JSONResponse({"ok": False, "error": "No waypoints provided"}, status_code=400)

    mission_items = [
        MissionItem(
            wp.lat,
            wp.lon,
            request.altitude,
            MISSION_SPEED_M_S,
            False,  # is_fly_through: stop at each waypoint (easier to watch)
            float("nan"),  # gimbal_pitch_deg: not used, no gimbal
            float("nan"),  # gimbal_yaw_deg: not used, no gimbal
            MissionItem.CameraAction.NONE,
            float("nan"),  # loiter_time_s: not used
            float("nan"),  # camera_photo_interval_s: not used
            MISSION_ACCEPTANCE_RADIUS_M,
            float("nan"),  # yaw_deg: let PX4 choose heading
            float("nan"),  # camera_photo_distance_m: not used
            MissionItem.VehicleAction.NONE,
        )
        for wp in request.waypoints
    ]
    mission_plan = MissionPlan(mission_items)

    async def _upload(drone: System) -> None:
        # Clear any previous mission first so old waypoints aren't merged in.
        await drone.mission.clear_mission()
        await drone.mission.upload_mission(mission_plan)

    return await _run_command(state, _upload)


@app.post("/api/{drone_id}/mission/start")
async def start_mission(drone_id: str) -> JSONResponse:
    """Start (or resume) the uploaded mission on one drone.

    PX4 typically requires the vehicle to be armed before a mission can
    start. If this fails because the vehicle isn't armed, the ActionError/
    MissionError message returned here will say so — arm (or takeoff) first
    and try again.
    """
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, lambda d: d.mission.start_mission())


@app.post("/api/{drone_id}/mission/pause")
async def pause_mission(drone_id: str) -> JSONResponse:
    """Pause the currently running mission on one drone in place."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state
    return await _run_command(state, lambda d: d.mission.pause_mission())


class GeofenceUploadRequest(BaseModel):
    points: list[Waypoint]


@app.post("/api/{drone_id}/geofence/upload")
async def upload_geofence(drone_id: str, request: GeofenceUploadRequest) -> JSONResponse:
    """Upload an inclusion geofence polygon to one drone.

    The polygon is an inclusion fence: PX4 should keep the vehicle inside
    it (the breach reaction is controlled by PX4's GF_ACTION parameter,
    e.g. warn/hold/RTL, and isn't changed here).
    """
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if len(request.points) < 3:
        return JSONResponse({"ok": False, "error": "A geofence polygon needs at least 3 points"}, status_code=400)

    polygon = Polygon(
        [Point(p.lat, p.lon) for p in request.points],
        FenceType.INCLUSION,
    )
    geofence_data = GeofenceData(polygons=[polygon], circles=[])

    async def _upload(drone: System) -> None:
        await drone.geofence.upload_geofence(geofence_data)
        # Also make sure PX4 reacts to breaches (Hold), otherwise the
        # fence is uploaded but enforcement may be a no-op/warning only.
        await drone.param.set_param_int("GF_ACTION", GF_ACTION_HOLD)

    return await _run_command(state, _upload)


@app.post("/api/{drone_id}/geofence/clear")
async def clear_geofence(drone_id: str) -> JSONResponse:
    """Remove all geofences stored on one drone and stop enforcing GF_ACTION."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    async def _clear(drone: System) -> None:
        await drone.geofence.clear_geofence()
        await drone.param.set_param_int("GF_ACTION", GF_ACTION_NONE)

    return await _run_command(state, _clear)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _velocity_setpoint(state: DroneState) -> VelocityBodyYawspeed:
    v = state.manual_velocity
    return VelocityBodyYawspeed(v["forward"], v["right"], v["down"], v["yaw_speed_deg_s"])


async def _manual_setpoint_loop(state: DroneState) -> None:
    """Stream the current manual velocity setpoint to PX4 at a fixed rate.

    PX4's offboard mode requires a steady stream of setpoints (faster than
    2 Hz) for as long as it's active, even when the desired velocity is zero
    (hover) — if the stream stops, PX4 falls back out of offboard mode. So
    this loop keeps running and re-sending state.manual_velocity, unchanged
    or not, until manual control is stopped.
    """
    period = 1.0 / MANUAL_SETPOINT_RATE_HZ
    while True:
        if state.drone is not None:
            try:
                await state.drone.offboard.set_velocity_body(_velocity_setpoint(state))
            except OffboardError:
                logger.exception("[%s] Failed to send manual setpoint", state.drone_id)
        await asyncio.sleep(period)


async def _stop_manual_task(state: DroneState) -> None:
    """Cancel the setpoint-streaming task, if running, and reset to hover."""
    state.manual_velocity.update(forward=0.0, right=0.0, down=0.0, yaw_speed_deg_s=0.0)

    if state.manual_task is not None:
        state.manual_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.manual_task
        state.manual_task = None


@app.post("/api/{drone_id}/manual/start")
async def manual_start(drone_id: str) -> JSONResponse:
    """Enable manual offboard control for one drone.

    Requires the vehicle to already be armed and airborne (e.g. via
    takeoff) — this endpoint does not arm or take off on its own.
    """
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if state.manual_task is not None:
        return JSONResponse({"ok": True})

    is_armed = await anext(state.drone.telemetry.armed())
    is_in_air = await anext(state.drone.telemetry.in_air())
    if not is_armed or not is_in_air:
        return JSONResponse(
            {"ok": False, "error": "Vehicle must be armed and airborne before enabling manual control"},
            status_code=400,
        )

    state.manual_velocity.update(forward=0.0, right=0.0, down=0.0, yaw_speed_deg_s=0.0)

    try:
        await state.drone.offboard.set_velocity_body(_velocity_setpoint(state))
        await state.drone.offboard.start()
    except OffboardError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    state.manual_task = asyncio.create_task(_manual_setpoint_loop(state))
    return JSONResponse({"ok": True})


class ManualCommand(BaseModel):
    forward: float = 0.0
    right: float = 0.0
    # Down in MAVSDK's body frame: positive means descend, negative means
    # climb. The frontend negates its "up" control before sending here.
    down: float = 0.0
    yaw_speed: float = 0.0


@app.post("/api/{drone_id}/manual/command")
async def manual_command(drone_id: str, command: ManualCommand) -> JSONResponse:
    """Update the manual velocity setpoint streamed to one drone.

    Values are clamped to sane limits and yaw_speed (rad/s, matching the
    rest of the API) is converted to degrees/s for MAVSDK's
    VelocityBodyYawspeed, which expects yawspeed_deg_s.
    """
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if state.manual_task is None:
        return JSONResponse({"ok": False, "error": "Manual control is not active"}, status_code=400)

    state.manual_velocity["forward"] = _clamp(command.forward, MANUAL_MAX_HORIZONTAL_M_S)
    state.manual_velocity["right"] = _clamp(command.right, MANUAL_MAX_HORIZONTAL_M_S)
    state.manual_velocity["down"] = _clamp(command.down, MANUAL_MAX_VERTICAL_M_S)
    yaw_speed_rad_s = _clamp(command.yaw_speed, MANUAL_MAX_YAW_RATE_RAD_S)
    state.manual_velocity["yaw_speed_deg_s"] = math.degrees(yaw_speed_rad_s)

    return JSONResponse({"ok": True})


@app.post("/api/{drone_id}/manual/stop")
async def manual_stop(drone_id: str) -> JSONResponse:
    """Disable manual offboard control for one drone and hand control back to PX4 modes."""
    state = _get_drone(drone_id)
    if isinstance(state, JSONResponse):
        return state

    if state.drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if state.manual_task is None:
        return JSONResponse({"ok": True})

    await _stop_manual_task(state)

    try:
        await state.drone.offboard.stop()
    except OffboardError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return JSONResponse({"ok": True})


class PlanRequest(BaseModel):
    prompt: str
    drone_id: str


@app.post("/api/plan")
async def plan_mission(request: PlanRequest) -> JSONResponse:
    """Turn a natural-language request into a structured mission plan for approval.

    Does NOT execute anything — the returned plan must be sent back to
    /api/plan/execute (after user approval) to actually fly it.
    """
    state = _get_drone(request.drone_id)
    if isinstance(state, JSONResponse):
        return state

    latest = hub.latest.get(state.drone_id)
    if not latest or latest.get("lat") is None or latest.get("lon") is None:
        return JSONResponse(
            {"ok": False, "error": "Drone position unknown — wait for a telemetry fix before planning"},
            status_code=503,
        )

    try:
        plan = await llm_planner.generate_plan(request.prompt, latest["lat"], latest["lon"])
    except llm_planner.PlanError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return JSONResponse({"ok": True, "plan": plan})


class PlanExecuteRequest(BaseModel):
    drone_id: str
    altitude: float
    waypoints: list[Waypoint]


@app.post("/api/plan/execute")
async def execute_plan(request: PlanExecuteRequest) -> JSONResponse:
    """Upload an approved plan as a mission and start it, reusing the existing mission flow."""
    upload_result = await upload_mission(
        request.drone_id, MissionUploadRequest(altitude=request.altitude, waypoints=request.waypoints)
    )
    if json.loads(upload_result.body).get("ok") is not True:
        return upload_result

    return await start_mission(request.drone_id)
