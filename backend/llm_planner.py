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


def _validate_plan(plan: _PlanResponse, current_lat: float, current_lon: float) -> dict:
    if not plan.waypoints:
        raise PlanError("Plan has no waypoints")

    if len(plan.waypoints) > MAX_WAYPOINTS:
        raise PlanError(f"Plan has too many waypoints ({len(plan.waypoints)} > {MAX_WAYPOINTS})")

    if not (MIN_ALTITUDE_M <= plan.altitude <= MAX_ALTITUDE_M):
        raise PlanError(f"Altitude {plan.altitude}m is outside the allowed range ({MIN_ALTITUDE_M}-{MAX_ALTITUDE_M}m)")

    for wp in plan.waypoints:
        if abs(wp.lat - current_lat) > MAX_COORD_DELTA_DEG or abs(wp.lon - current_lon) > MAX_COORD_DELTA_DEG:
            raise PlanError("Plan waypoints are too far from the drone's current position")

    return {
        "summary": plan.summary,
        "altitude": plan.altitude,
        "waypoints": [{"lat": wp.lat, "lon": wp.lon} for wp in plan.waypoints],
        "notes": plan.notes or "",
    }
