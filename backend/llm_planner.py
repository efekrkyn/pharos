"""Natural-language mission planning via the DeepSeek API.

Turns a plain-language recon/navigation request into a structured mission
plan (altitude + waypoints) for one drone. The plan is validated server-side
and returned to the frontend for human approval — this module never executes
anything on a vehicle.
"""

import json
import logging
import math
import os

import httpx
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

# Swarm planning: the LLM only picks high-level area parameters; Python
# deterministically splits the area into non-overlapping per-drone sectors
# and generates the scan pattern (see build_swarm_assignments()).
SWARM_MIN_ALTITUDE_M = 5.0
SWARM_MAX_ALTITUDE_M = 120.0
SWARM_DEFAULT_ALTITUDE_M = 30.0

MIN_AREA_SIZE_M = 20.0
MAX_AREA_SIZE_M = 2000.0
DEFAULT_AREA_SIZE_M = 150.0

# Spacing between lawnmower lanes within a drone's sector, in meters.
LANE_SPACING_M = 40.0

# Project's home location, used as the center for swarm area scans when the
# request doesn't name a place at all (or geocoding is skipped).
HOME_LAT = 39.9228214
HOME_LON = 32.8618589

# Geocoding (place name -> coordinates) via OpenStreetMap Nominatim. Nominatim
# requires a unique User-Agent and asks clients not to hammer the service —
# generate_swarm_plan() makes at most one request per plan.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "pharos-gcs/1.0"
NOMINATIM_TIMEOUT_S = 5.0

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
into a structured JSON AREA SPEC describing a navigation/surveillance task to \
be carried out by a group of drones. You must respond with a single JSON \
object and nothing else — no markdown, no commentary outside the JSON.

You do NOT compute per-drone waypoints and you do NOT divide the area \
yourself, and you do NOT determine GPS coordinates yourself. A separate \
deterministic step in the ground control software geocodes any named place, \
splits the area into non-overlapping sectors, one per drone, and generates \
each drone's scan pattern from your area spec. Your job is only to determine \
the overall area, pattern, altitude, and the place name (if any) from the \
request.

The user message tells you how many drones are available and lists their \
current positions.

If the request is a valid navigation/observation/recon task, respond with \
exactly this JSON schema:
{
  "summary": "<one-line human description of the overall mission, in the same language as the request>",
  "pattern": "grid_scan" | "patrol" | "loiter",
  "location_query": "<the place name as the user said it, e.g. 'Kurtulus Parki Ankara', or an empty string if no place was named>",
  "area_size_m": <number, side length in meters of the square area to cover>,
  "altitude": <number, meters relative, between 5 and 120>,
  "num_drones": <integer, number of drones listed in the user message>
}

Use "grid_scan" for area/region scans, grid searches, and "split into regions \
and scan" requests. Use "patrol" for perimeter patrols. Use "loiter" for \
simple hold/observe-position tasks.

NEVER invent or guess latitude/longitude coordinates yourself — coordinates \
come only from geocoding "location_query" or from a home-location default \
when no place is named. If the request names a place (e.g. "TED University", \
"Kurtulus Parki", "the campus"), put it in "location_query" exactly as \
phrased (add a city like "Ankara" if it helps disambiguate). If the request \
doesn't name a specific place, set "location_query" to an empty string.

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


class _SwarmAreaSpec(BaseModel):
    summary: str = "Swarm mission plan"
    pattern: str = "grid_scan"
    location_query: str = ""
    area_size_m: float = DEFAULT_AREA_SIZE_M
    altitude: float = SWARM_DEFAULT_ALTITUDE_M
    num_drones: int = 0


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


def _offset_latlon(lat0: float, lon0: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Offset a lat/lon by a small north/east displacement in meters (flat-earth approximation)."""
    dlat = north_m / 111320.0
    dlon = east_m / (111320.0 * math.cos(math.radians(lat0)))
    return (lat0 + dlat, lon0 + dlon)


def build_swarm_assignments(
    center_lat: float,
    center_lon: float,
    area_size_m: float,
    num_drones: int,
    altitude: float,
    drone_ids: list[str],
    pattern: str = "grid_scan",
    lane_spacing_m: float = LANE_SPACING_M,
) -> list[dict]:
    """Deterministically split a square area into non-overlapping per-drone sectors.

    The area (side = area_size_m, centered on center_lat/center_lon) is split into
    `num_drones` vertical strips along the east axis, in order of `drone_ids`. Each
    strip's east-range is disjoint from the others, so sectors can't overlap by
    construction. Within each strip:
    - "grid_scan" (default): a boustrophedon (lawnmower) pattern of north-south
      lanes stepped east, alternating sweep direction each lane.
    - "patrol": a perimeter loop of the strip's four corners.
    - "loiter": a single waypoint at the center of the strip.

    Returns a list of {"drone_id", "altitude", "waypoints": [{"lat", "lon"}, ...]}.
    """
    half = area_size_m / 2.0
    strip_w = area_size_m / num_drones

    assignments = []
    for i, drone_id in enumerate(drone_ids):
        east_start = -half + i * strip_w
        east_end = east_start + strip_w

        if pattern == "loiter":
            center_east = (east_start + east_end) / 2.0
            lat, lon = _offset_latlon(center_lat, center_lon, 0.0, center_east)
            waypoints = [{"lat": lat, "lon": lon}]
        elif pattern == "patrol":
            corners_en = [
                (-half, east_start),
                (-half, east_end),
                (half, east_end),
                (half, east_start),
                (-half, east_start),
            ]
            waypoints = [
                {"lat": lat, "lon": lon}
                for lat, lon in (_offset_latlon(center_lat, center_lon, north, east) for north, east in corners_en)
            ]
        else:
            lanes = max(2, math.ceil(strip_w / lane_spacing_m))
            waypoints = []
            for j in range(lanes):
                east = east_start + j * (strip_w / (lanes - 1))
                norths = (-half, half) if j % 2 == 0 else (half, -half)
                for north in norths:
                    lat, lon = _offset_latlon(center_lat, center_lon, north, east)
                    waypoints.append({"lat": lat, "lon": lon})

        assignments.append({"drone_id": drone_id, "altitude": altitude, "waypoints": waypoints})

    return assignments


async def geocode(query: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon) via OpenStreetMap Nominatim, or None.

    Returns None (and logs why) if `query` is empty, Nominatim returns no
    result, or the request fails/times out. Biased toward Turkey since this
    project operates in Ankara: results are restricted to countrycodes=tr,
    and if the first lookup finds nothing, a second attempt appends ", Ankara"
    to the query. Makes at most two requests, per Nominatim's usage policy.
    """
    query = query.strip()
    if not query:
        return None

    headers = {"User-Agent": NOMINATIM_USER_AGENT}
    params = {"q": query, "format": "json", "limit": 1, "countrycodes": "tr"}

    async def _lookup(q: str) -> tuple[float, float] | None:
        try:
            async with httpx.AsyncClient(timeout=NOMINATIM_TIMEOUT_S) as client:
                response = await client.get(NOMINATIM_URL, params={**params, "q": q}, headers=headers)
                response.raise_for_status()
                results = response.json()
        except Exception:
            logger.exception("Nominatim geocoding request failed for %r", q)
            return None

        if not results:
            return None

        try:
            return float(results[0]["lat"]), float(results[0]["lon"])
        except (KeyError, ValueError, TypeError):
            logger.warning("Nominatim returned an unexpected result for %r: %r", q, results)
            return None

    result = await _lookup(query)
    if result is not None:
        return result

    if "ankara" not in query.lower():
        result = await _lookup(f"{query}, Ankara")
        if result is not None:
            return result

    logger.warning("Nominatim found no result for %r", query)
    return None


async def generate_swarm_plan(prompt: str, drones: list[dict]) -> dict:
    """Ask DeepSeek for a high-level area spec, then deterministically split it across drones.

    `drones` is a list of {"drone_id", "label", "lat", "lon"} for the drones to
    assign tasks to. The LLM only chooses the area/pattern/altitude — the actual
    per-drone, non-overlapping sectors and waypoints are computed in Python by
    build_swarm_assignments(). Returns a dict with "summary" and "assignments"
    (a list of {"drone_id", "altitude", "waypoints"}), the same schema as before.
    Raises PlanError if the request fails or the LLM refuses.
    """
    client = _get_client()

    drones_description = "\n".join(
        f"- drone_id={d['drone_id']} ({d['label']}): lat={d['lat']}, lon={d['lon']}" for d in drones
    )
    user_message = f"Available drones ({len(drones)}):\n{drones_description}\n\nRequest: {prompt}"

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
    logger.info("Swarm planner raw LLM response: %r", content)

    if not content:
        raise PlanError("DeepSeek returned an empty response")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PlanError(f"DeepSeek returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanError("DeepSeek returned an unexpected response shape (not a JSON object)")

    if data.get("refused"):
        raise PlanError(data.get("reason") or "The planner refused this request.")

    try:
        spec = _SwarmAreaSpec.model_validate(data)
    except ValidationError as exc:
        raise PlanError(f"DeepSeek returned an invalid plan: {exc}") from exc

    # Every field of _SwarmAreaSpec has a default, so spec is always usable even
    # if the LLM omitted fields. Still clamp/normalize values that are in range
    # but nonsensical, and never trust the LLM's drone count or pattern name.
    pattern = spec.pattern if spec.pattern in ("grid_scan", "patrol", "loiter") else "grid_scan"
    altitude = max(SWARM_MIN_ALTITUDE_M, min(SWARM_MAX_ALTITUDE_M, spec.altitude))
    area_size_m = max(MIN_AREA_SIZE_M, min(MAX_AREA_SIZE_M, spec.area_size_m))
    drone_ids = [d["drone_id"] for d in drones]

    location_query = spec.location_query.strip()
    if location_query:
        coords = await geocode(location_query)
        if coords is None:
            raise PlanError(
                f"Could not locate '{location_query}'. Please name a known place or pick a point on the map."
            )
        center_lat, center_lon = coords
    else:
        center_lat, center_lon = HOME_LAT, HOME_LON

    assignments = build_swarm_assignments(
        center_lat,
        center_lon,
        area_size_m,
        len(drone_ids),
        altitude,
        drone_ids,
        pattern=pattern,
    )

    return {"summary": spec.summary, "assignments": assignments}
