"""Natural-language mission planning via the DeepSeek API.

Turns a plain-language recon/navigation request into a structured mission
plan (altitude + waypoints) for one drone. The plan is validated server-side
and returned to the frontend for human approval — this module never executes
anything on a vehicle.
"""

import json
import logging
import os

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Easy to change if this model becomes unavailable — fall back to
# "deepseek-chat" in that case.
DEEPSEEK_MODEL = "deepseek-v4-flash"

MAX_TOKENS = 4000

MIN_ALTITUDE_M = 2.0
MAX_ALTITUDE_M = 50.0
MAX_WAYPOINTS = 20

# Waypoints further than this from the drone's current position (in degrees,
# roughly ~2km) are rejected as implausible for a campus-scale recon mission.
MAX_COORD_DELTA_DEG = 0.02

SYSTEM_PROMPT = """You are a mission planner for a PX4 drone ground control \
station running in SIMULATION (PX4 SITL) only.

Your ONLY job is to convert a natural-language request (Turkish or English) \
into a structured JSON navigation/surveillance mission plan for ONE drone: \
waypoint routes, grid/area scans, patrol loops, and go-to/loiter tasks. You \
must respond with a single JSON object and nothing else — no markdown, no \
commentary outside the JSON.

If the request is a valid navigation/observation/recon task, respond with \
exactly this JSON schema:
{
  "summary": "<one-line human description of what the mission will do>",
  "altitude": <number, meters relative to the drone's current position, between 2 and 50>,
  "waypoints": [ {"lat": <number>, "lon": <number>}, ... ],
  "notes": "<optional extra info, or an empty string>"
}

Generate between 2 and 20 waypoints, in flight order, forming the requested \
route, grid scan, or patrol loop. Keep all waypoints within roughly 1-2 km of \
the drone's current position, which is given in the user message.

If the request asks for anything related to weapons, targeting, strikes, \
attacking, or intercepting/following another aircraft or vehicle for an \
offensive purpose, or any other task that is not navigation/observation, you \
MUST refuse. Respond with exactly this JSON schema instead:
{
  "refused": true,
  "reason": "<short explanation, asking the user for a navigation/observation task instead>"
}

Never include anything other than one of these two JSON objects."""

SWARM_SYSTEM_PROMPT = """You are a mission planner for a swarm of PX4 drones in \
a ground control station running in SIMULATION (PX4 SITL) only.

Your ONLY job is to convert a natural-language request (Turkish or English) \
into a structured JSON navigation/surveillance mission plan that DIVIDES the \
requested task across the given drones: waypoint routes, grid/area scans, \
patrol loops, and go-to/loiter tasks. You must respond with a single JSON \
object and nothing else — no markdown, no commentary outside the JSON.

The user message lists the available drones, each with a drone_id and current \
position. Divide the task sensibly across ALL listed drones — e.g. "scan the \
campus with all drones" should split the area into one non-overlapping sector \
per drone; "patrol the perimeter" should divide the perimeter into one segment \
per drone. Every listed drone must get an assignment.

If the request is a valid navigation/observation/recon task, respond with \
exactly this JSON schema:
{
  "summary": "<one-line human description of the overall mission and how it's split>",
  "assignments": [
    {
      "drone_id": "<must match one of the given drone_ids>",
      "altitude": <number, meters relative to that drone's current position, between 2 and 50>,
      "waypoints": [ {"lat": <number>, "lon": <number>}, ... ]
    },
    ...
  ]
}

Generate between 2 and 20 waypoints per drone, in flight order, forming that \
drone's portion of the route, grid scan, or patrol loop. Keep each drone's \
waypoints within roughly 1-2 km of that drone's current position, given in \
the user message.

If the request asks for anything related to weapons, targeting, strikes, \
attacking, or intercepting/following another aircraft or vehicle for an \
offensive purpose, or any other task that is not navigation/observation, you \
MUST refuse. Respond with exactly this JSON schema instead:
{
  "refused": true,
  "reason": "<short explanation, asking the user for a navigation/observation task instead>"
}

Never include anything other than one of these two JSON objects."""


class PlanError(Exception):
    """Raised when a plan can't be generated or fails validation."""


class _Waypoint(BaseModel):
    lat: float
    lon: float


class _PlanResponse(BaseModel):
    summary: str
    altitude: float
    waypoints: list[_Waypoint]
    notes: str | None = None


class _AssignmentResponse(BaseModel):
    drone_id: str
    altitude: float
    waypoints: list[_Waypoint]


class _SwarmPlanResponse(BaseModel):
    summary: str
    assignments: list[_AssignmentResponse]


def _get_client() -> AsyncOpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise PlanError(
            "DEEPSEEK_API_KEY is not set. Add it to a .env file in the project root "
            "(see .env.example) and restart the server."
        )
    return AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)


async def generate_plan(prompt: str, current_lat: float, current_lon: float) -> dict:
    """Ask DeepSeek for a structured mission plan and validate it.

    Returns a dict with "summary", "altitude", "waypoints", and "notes".
    Raises PlanError if the request fails, the LLM refuses, or the plan
    fails validation (out-of-range altitude, too many/few waypoints,
    implausible coordinates).
    """
    client = _get_client()

    user_message = (
        f"Drone's current position: lat={current_lat}, lon={current_lon}. "
        "Altitude in the plan is relative meters above this position. "
        f"Request: {prompt}"
    )

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
        )
    except Exception as exc:
        logger.exception("DeepSeek API request failed")
        raise PlanError(f"DeepSeek API request failed: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise PlanError("DeepSeek returned an empty response")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlanError(f"DeepSeek returned invalid JSON: {exc}") from exc

    if data.get("refused"):
        raise PlanError(data.get("reason") or "The planner refused this request.")

    try:
        plan = _PlanResponse.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"DeepSeek returned an invalid plan: {exc}") from exc

    return _validate_plan(plan, current_lat, current_lon)


def _validate_waypoints(
    altitude: float, waypoints: list[_Waypoint], current_lat: float, current_lon: float
) -> None:
    if not waypoints:
        raise PlanError("Plan has no waypoints")

    if len(waypoints) > MAX_WAYPOINTS:
        raise PlanError(f"Plan has too many waypoints ({len(waypoints)} > {MAX_WAYPOINTS})")

    if not (MIN_ALTITUDE_M <= altitude <= MAX_ALTITUDE_M):
        raise PlanError(f"Altitude {altitude}m is outside the allowed range ({MIN_ALTITUDE_M}-{MAX_ALTITUDE_M}m)")

    for wp in waypoints:
        if abs(wp.lat - current_lat) > MAX_COORD_DELTA_DEG or abs(wp.lon - current_lon) > MAX_COORD_DELTA_DEG:
            raise PlanError("Plan waypoints are too far from the drone's current position")


def _validate_plan(plan: _PlanResponse, current_lat: float, current_lon: float) -> dict:
    _validate_waypoints(plan.altitude, plan.waypoints, current_lat, current_lon)

    return {
        "summary": plan.summary,
        "altitude": plan.altitude,
        "waypoints": [{"lat": wp.lat, "lon": wp.lon} for wp in plan.waypoints],
        "notes": plan.notes or "",
    }


async def generate_swarm_plan(prompt: str, drones: list[dict]) -> dict:
    """Ask DeepSeek to divide a task across multiple drones and validate the result.

    `drones` is a list of {"drone_id", "label", "lat", "lon"} for the drones to
    assign tasks to. Returns a dict with "summary" and "assignments" (a list of
    {"drone_id", "altitude", "waypoints"}). Raises PlanError if the request
    fails, the LLM refuses, or any assignment fails validation.
    """
    client = _get_client()

    drones_description = "\n".join(
        f"- drone_id={d['drone_id']} ({d['label']}): lat={d['lat']}, lon={d['lon']}" for d in drones
    )
    user_message = (
        f"Available drones:\n{drones_description}\n\n"
        "Altitude in each assignment is relative meters above that drone's current position. "
        f"Request: {prompt}"
    )

    try:
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SWARM_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_tokens=MAX_TOKENS,
        )
    except Exception as exc:
        logger.exception("DeepSeek API request failed")
        raise PlanError(f"DeepSeek API request failed: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise PlanError("DeepSeek returned an empty response")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlanError(f"DeepSeek returned invalid JSON: {exc}") from exc

    if data.get("refused"):
        raise PlanError(data.get("reason") or "The planner refused this request.")

    try:
        plan = _SwarmPlanResponse.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"DeepSeek returned an invalid plan: {exc}") from exc

    positions = {d["drone_id"]: (d["lat"], d["lon"]) for d in drones}

    if not plan.assignments:
        raise PlanError("Plan has no assignments")

    seen_drone_ids: set[str] = set()
    for assignment in plan.assignments:
        if assignment.drone_id not in positions:
            raise PlanError(f"Plan assigned an unknown drone '{assignment.drone_id}'")
        if assignment.drone_id in seen_drone_ids:
            raise PlanError(f"Plan assigned drone '{assignment.drone_id}' more than once")
        seen_drone_ids.add(assignment.drone_id)

        lat, lon = positions[assignment.drone_id]
        _validate_waypoints(assignment.altitude, assignment.waypoints, lat, lon)

    return {
        "summary": plan.summary,
        "assignments": [
            {
                "drone_id": a.drone_id,
                "altitude": a.altitude,
                "waypoints": [{"lat": wp.lat, "lon": wp.lon} for wp in a.waypoints],
            }
            for a in plan.assignments
        ],
    }
