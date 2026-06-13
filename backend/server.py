"""FastAPI server that streams live PX4 telemetry to browser clients."""

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mavsdk import System
from mavsdk.action import ActionError
from mavsdk.mission import MissionError, MissionItem, MissionPlan
from mavsdk.offboard import OffboardError, VelocityBodyYawspeed
from pydantic import BaseModel

from drone.connection import ConnectionTimeoutError, connect_drone
from telemetry_hub import WAITING_STATE, TelemetryHub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TAKEOFF_ALTITUDE_M = 5.0

# Cruise speed between waypoints and how close the vehicle must get to a
# waypoint before it's considered reached. Reasonable defaults for SITL.
MISSION_SPEED_M_S = 5.0
MISSION_ACCEPTANCE_RADIUS_M = 2.0

# Manual control: how fast we re-send velocity setpoints, and the speed
# limits applied to incoming /api/manual/command values.
MANUAL_SETPOINT_RATE_HZ = 10
MANUAL_MAX_HORIZONTAL_M_S = 8.0
MANUAL_MAX_VERTICAL_M_S = 4.0
MANUAL_MAX_YAW_RATE_RAD_S = 2.0

hub = TelemetryHub()

# The single shared drone connection, set once telemetry_task() connects.
# REST command endpoints reuse this instead of opening their own connections.
drone: System | None = None

# Current manual-control velocity setpoint, in MAVSDK's forward/right/down
# body frame (down positive = descending). Updated by /api/manual/command
# and streamed continuously to PX4 by _manual_setpoint_loop() while active.
manual_velocity = {"forward": 0.0, "right": 0.0, "down": 0.0, "yaw_speed_deg_s": 0.0}

# The background task streaming manual_velocity to PX4, or None if manual
# control (offboard mode) isn't active.
manual_task: asyncio.Task | None = None

# Latest mission progress, merged into every telemetry broadcast so existing
# clients keep working unchanged and new clients get progress "for free".
# total == 0 means no mission is active.
mission_state = {"mission_current": 0, "mission_total": 0}


async def telemetry_task() -> None:
    """Maintain a single drone connection and broadcast position updates.

    Runs for the lifetime of the app. If no drone is found, connect_drone()
    times out (10s) and we simply retry, leaving clients in "waiting" state.
    If a connected drone's telemetry stream ends, we go back to "waiting"
    and try to reconnect.
    """
    global drone

    while True:
        try:
            drone = await connect_drone()
        except ConnectionTimeoutError as exc:
            logger.warning("%s", exc)
            continue

        try:
            async for position in drone.telemetry.position():
                await hub.broadcast(
                    {
                        "status": "connected",
                        "lat": position.latitude_deg,
                        "lon": position.longitude_deg,
                        "abs_alt": position.absolute_altitude_m,
                        "rel_alt": position.relative_altitude_m,
                        **mission_state,
                    }
                )
        except Exception:
            logger.exception("Telemetry stream ended, will retry connection")
            drone = None
            await _stop_manual_task()
            mission_state.update(mission_current=0, mission_total=0)
            await hub.broadcast({**WAITING_STATE, **mission_state})


async def mission_progress_task() -> None:
    """Track mission_progress() on the shared connection and broadcast it.

    Merges into the same telemetry messages sent by telemetry_task() (via
    mission_state) so existing clients don't need a new message type. Retries
    quietly whenever there's no drone or the stream ends.
    """
    while True:
        current_drone = drone
        if current_drone is None:
            await asyncio.sleep(1)
            continue

        try:
            stream = current_drone.mission.mission_progress()
            while True:
                # If telemetry_task reconnected with a new drone/connection
                # while we were subscribed to the old one, this stream may
                # never yield again — bail out and resubscribe on the new one.
                if drone is not current_drone:
                    break
                try:
                    progress = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                mission_state.update(mission_current=progress.current, mission_total=progress.total)
                await hub.broadcast({**hub.latest, **mission_state})
        except StopAsyncIteration:
            await asyncio.sleep(1)
        except Exception:
            logger.exception("Mission progress stream ended, will retry")
            await asyncio.sleep(1)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the shared background tasks on startup, cancel them on shutdown."""
    tasks = [asyncio.create_task(telemetry_task()), asyncio.create_task(mission_progress_task())]
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


async def _run_command(action) -> JSONResponse:
    """Run an MAVSDK action coroutine and translate the outcome to JSON.

    `action` is a no-arg callable returning the action coroutine, so it's
    only evaluated after we've confirmed a drone is connected.

    Returns 503 if no drone is connected, 400 if PX4 rejects the command
    (e.g. failed preflight checks for arm), and 200 on success.
    """
    if drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    try:
        await action()
    except (ActionError, MissionError, OffboardError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return {"ok": True}


@app.post("/api/arm")
async def arm() -> JSONResponse:
    """Arm the vehicle. May fail if PX4 preflight checks haven't passed yet."""
    return await _run_command(lambda: drone.action.arm())


@app.post("/api/takeoff")
async def takeoff() -> JSONResponse:
    """Arm (if needed) and take off to TAKEOFF_ALTITUDE_M.

    PX4 requires the vehicle to be armed before takeoff() is accepted, so we
    arm first. If it's already armed, arm() is a harmless no-op/ActionError
    that we ignore here.
    """
    if drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    with contextlib.suppress(ActionError):
        await drone.action.arm()

    async def _takeoff():
        await drone.action.set_takeoff_altitude(TAKEOFF_ALTITUDE_M)
        await drone.action.takeoff()

    return await _run_command(_takeoff)


@app.post("/api/land")
async def land() -> JSONResponse:
    """Command the vehicle to land at its current position."""
    return await _run_command(lambda: drone.action.land())


@app.post("/api/rtl")
async def rtl() -> JSONResponse:
    """Command the vehicle to return to its launch position and land."""
    return await _run_command(lambda: drone.action.return_to_launch())


class Waypoint(BaseModel):
    lat: float
    lon: float


class MissionUploadRequest(BaseModel):
    altitude: float
    waypoints: list[Waypoint]


@app.post("/api/mission/upload")
async def upload_mission(request: MissionUploadRequest) -> JSONResponse:
    """Build a MissionPlan from the given waypoints and upload it to PX4."""
    if drone is None:
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

    async def _upload():
        # Clear any previous mission first so old waypoints aren't merged in.
        await drone.mission.clear_mission()
        await drone.mission.upload_mission(mission_plan)

    return await _run_command(_upload)


@app.post("/api/mission/start")
async def start_mission() -> JSONResponse:
    """Start (or resume) the uploaded mission.

    PX4 typically requires the vehicle to be armed before a mission can
    start. If this fails because the vehicle isn't armed, the ActionError/
    MissionError message returned here will say so — arm (or takeoff) first
    and try again.
    """
    return await _run_command(lambda: drone.mission.start_mission())


@app.post("/api/mission/pause")
async def pause_mission() -> JSONResponse:
    """Pause the currently running mission in place."""
    return await _run_command(lambda: drone.mission.pause_mission())


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _velocity_setpoint() -> VelocityBodyYawspeed:
    v = manual_velocity
    return VelocityBodyYawspeed(v["forward"], v["right"], v["down"], v["yaw_speed_deg_s"])


async def _manual_setpoint_loop() -> None:
    """Stream the current manual velocity setpoint to PX4 at a fixed rate.

    PX4's offboard mode requires a steady stream of setpoints (faster than
    2 Hz) for as long as it's active, even when the desired velocity is zero
    (hover) — if the stream stops, PX4 falls back out of offboard mode. So
    this loop keeps running and re-sending manual_velocity, unchanged or not,
    until manual control is stopped.
    """
    period = 1.0 / MANUAL_SETPOINT_RATE_HZ
    while True:
        if drone is not None:
            try:
                await drone.offboard.set_velocity_body(_velocity_setpoint())
            except OffboardError:
                logger.exception("Failed to send manual setpoint")
        await asyncio.sleep(period)


async def _stop_manual_task() -> None:
    """Cancel the setpoint-streaming task, if running, and reset to hover."""
    global manual_task

    manual_velocity.update(forward=0.0, right=0.0, down=0.0, yaw_speed_deg_s=0.0)

    if manual_task is not None:
        manual_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manual_task
        manual_task = None


@app.post("/api/manual/start")
async def manual_start() -> JSONResponse:
    """Enable manual offboard control.

    Requires the vehicle to already be armed and airborne (e.g. via
    /api/takeoff) — this endpoint does not arm or take off on its own.
    """
    global manual_task

    if drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if manual_task is not None:
        return {"ok": True}

    is_armed = await anext(drone.telemetry.armed())
    is_in_air = await anext(drone.telemetry.in_air())
    if not is_armed or not is_in_air:
        return JSONResponse(
            {"ok": False, "error": "Vehicle must be armed and airborne before enabling manual control"},
            status_code=400,
        )

    manual_velocity.update(forward=0.0, right=0.0, down=0.0, yaw_speed_deg_s=0.0)

    try:
        await drone.offboard.set_velocity_body(_velocity_setpoint())
        await drone.offboard.start()
    except OffboardError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    manual_task = asyncio.create_task(_manual_setpoint_loop())
    return {"ok": True}


class ManualCommand(BaseModel):
    forward: float = 0.0
    right: float = 0.0
    # Down in MAVSDK's body frame: positive means descend, negative means
    # climb. The frontend negates its "up" control before sending here.
    down: float = 0.0
    yaw_speed: float = 0.0


@app.post("/api/manual/command")
async def manual_command(command: ManualCommand) -> JSONResponse:
    """Update the manual velocity setpoint streamed to PX4.

    Values are clamped to sane limits and yaw_speed (rad/s, matching the
    rest of the API) is converted to degrees/s for MAVSDK's
    VelocityBodyYawspeed, which expects yawspeed_deg_s.
    """
    if drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if manual_task is None:
        return JSONResponse({"ok": False, "error": "Manual control is not active"}, status_code=400)

    manual_velocity["forward"] = _clamp(command.forward, MANUAL_MAX_HORIZONTAL_M_S)
    manual_velocity["right"] = _clamp(command.right, MANUAL_MAX_HORIZONTAL_M_S)
    manual_velocity["down"] = _clamp(command.down, MANUAL_MAX_VERTICAL_M_S)
    yaw_speed_rad_s = _clamp(command.yaw_speed, MANUAL_MAX_YAW_RATE_RAD_S)
    manual_velocity["yaw_speed_deg_s"] = math.degrees(yaw_speed_rad_s)

    return {"ok": True}


@app.post("/api/manual/stop")
async def manual_stop() -> JSONResponse:
    """Disable manual offboard control and hand control back to PX4 modes."""
    if drone is None:
        return JSONResponse({"ok": False, "error": "Drone not connected"}, status_code=503)

    if manual_task is None:
        return {"ok": True}

    await _stop_manual_task()

    try:
        await drone.offboard.stop()
    except OffboardError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    return {"ok": True}
