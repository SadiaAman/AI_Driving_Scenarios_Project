"""
LLM-powered natural-language scenario parser for Variant B.

Converts a free-text scenario description into structured parameters using
Gemini's constrained JSON output. Called by /generate-from-text in main.py
before the rule-based parse_scenario_text() fallback.

On any technical failure (missing key, SDK absent, API error) the function
returns (None, None, None) so the caller falls back transparently — generation
never breaks.
"""

import os
from typing import Literal, Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# SDK detection
# ---------------------------------------------------------------------------
try:
    from google import genai as _genai
    _GEMINI_SDK = True
except Exception:
    _GEMINI_SDK = False


def _is_real_key(value: str) -> bool:
    return bool(value) and "REPLACE_WITH_YOUR" not in value.upper()


_GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

LLM_AVAILABLE = _GEMINI_SDK and _is_real_key(_GEMINI_KEY)
PROVIDER_LABEL = "Gemini" if LLM_AVAILABLE else "unavailable"


# ---------------------------------------------------------------------------
# Constrained output schema — Gemini's response_schema keeps it to valid enums
# ---------------------------------------------------------------------------
class ScenarioParsed(BaseModel):
    egoSpeed: Optional[float] = None
    trafficVehicles: Optional[int] = None
    timeOfDay: Optional[Literal["Day", "Night"]] = None
    weather: Optional[Literal["clear", "rain", "fog", "snow"]] = None
    npcType: Optional[Literal["car", "truck", "pedestrian", "cyclist"]] = None
    npcSpeed: Optional[float] = None
    npcBehavior: Optional[Literal[
        "brakes suddenly",
        "changes lane suddenly",
        "cuts in front of ego",
        "crosses the road",
    ]] = None
    egoResponse: Optional[Literal[
        "brakes immediately",
        "slows down",
        "steers to avoid",
        "keeps lane and reduces speed",
    ]] = None
    confidence: float = 1.0
    missingFields: Optional[list] = None
    unsupportedElements: Optional[list] = None
    clarificationMessage: Optional[str] = None


# ---------------------------------------------------------------------------
# System instructions
# ---------------------------------------------------------------------------
_SYSTEM = """\
# ROLE
You are a structured-parameter extractor for an OpenSCENARIO driving-scenario
generator used with the esmini simulator. You convert a user's natural-language
scenario description into a precise JSON object.

# TASK
Read the description and extract the scenario parameters. Use ONLY the allowed
values exactly as written. Set a field to null when the description does not
mention or clearly imply it.

# PARAMETERS AND ALLOWED VALUES
- egoSpeed: integer km/h, realistic road speeds only (1-130). REQUIRED.
- trafficVehicles: integer 0-3. Background traffic vehicles. Default 0 if not mentioned.
- timeOfDay: "Day" or "Night". Default "Day" if not mentioned.
- weather: "clear", "rain", "fog", or "snow". Default "clear" if not mentioned.
- npcType: "car", "truck", "pedestrian", or "cyclist". REQUIRED.
- npcSpeed: number km/h (1-130). Defaults: pedestrian->4, cyclist->10, car->30, truck->20.
- npcBehavior: REQUIRED. Choose ONE exactly:
    "brakes suddenly"       (car or truck only)
    "changes lane suddenly" (car or truck only)
    "cuts in front of ego"  (car or truck only)
    "crosses the road"      (pedestrian or cyclist only)
- egoResponse: REQUIRED. Choose ONE exactly:
    "brakes immediately"
    "slows down"
    "steers to avoid"
    "keeps lane and reduces speed"
- confidence: 0.0-1.0. Your confidence in the extraction.
- missingFields: list of REQUIRED field names you could not extract (strings).
- unsupportedElements: list of actors/elements not in the supported list (strings).
- clarificationMessage: short friendly message if the user needs to fix something; null otherwise.

# CRITICAL RULES
- Pedestrians and cyclists ONLY use "crosses the road". Never assign any other npcBehavior.
- Cars and trucks NEVER use "crosses the road". Pick the closest valid vehicle behaviour instead.
- Actor not in supported list -> add to unsupportedElements, set npcType to null.
- egoSpeed outside 1-130 -> set to null, add "egoSpeed" to missingFields.
- Output JSON only. No explanation. No markdown.

# EXAMPLES
Input: "The ego vehicle is travelling at 50 km/h during day. A pedestrian crosses the road, and the ego vehicle brakes immediately."
Output: {"egoSpeed":50,"trafficVehicles":0,"timeOfDay":"Day","weather":"clear","npcType":"pedestrian","npcSpeed":4,"npcBehavior":"crosses the road","egoResponse":"brakes immediately","confidence":0.99,"missingFields":[],"unsupportedElements":[],"clarificationMessage":null}

Input: "a car"
Output: {"egoSpeed":null,"trafficVehicles":0,"timeOfDay":"Day","weather":"clear","npcType":"car","npcSpeed":30,"npcBehavior":null,"egoResponse":null,"confidence":0.2,"missingFields":["egoSpeed","npcBehavior","egoResponse"],"unsupportedElements":[],"clarificationMessage":"Please describe the ego speed (e.g. 50 km/h), what the car does (e.g. brakes suddenly), and how the ego vehicle responds (e.g. brakes immediately)."}
"""

# ---------------------------------------------------------------------------
# NPC speed defaults (km/h) per actor type
# ---------------------------------------------------------------------------
_NPC_SPEED_DEFAULTS: dict = {
    "pedestrian": 4.0,
    "cyclist": 10.0,
    "car": 30.0,
    "truck": 20.0,
}


def _validate(p: ScenarioParsed, raw_text: str):
    """
    Validate Gemini's parsed output and apply safe defaults.

    Returns:
      (request_kwargs_dict, parsed_dict, [])    — all required fields valid
      (None, parsed_dict, [str, ...])           — missing or invalid required fields
    """
    messages: list = []

    # Unsupported actors (defensive: list may be None from Gemini)
    for elem in (p.unsupportedElements or []):
        messages.append(
            f"Unsupported actor: {elem}. Supported actors are car, truck, pedestrian, and cyclist."
        )

    # Ego speed range check
    ego_speed = int(p.egoSpeed) if p.egoSpeed is not None else None
    if ego_speed is not None and not (1 <= ego_speed <= 130):
        messages.append(
            "Ego speed is outside the supported range (1–130 km/h). Please use a realistic road speed."
        )
        ego_speed = None

    npc_type = p.npcType
    npc_behavior = p.npcBehavior
    ego_response = p.egoResponse

    # Behaviour / actor type cross-validation
    if npc_type in ("pedestrian", "cyclist") and npc_behavior and npc_behavior != "crosses the road":
        messages.append(
            f"The behaviour '{npc_behavior}' is not suitable for {npc_type}. "
            "Please use 'crosses the road' or choose a vehicle actor."
        )
        npc_behavior = None
    if npc_type in ("car", "truck") and npc_behavior == "crosses the road":
        messages.append(
            f"The behaviour 'crosses the road' is not suitable for {npc_type}. "
            "Please use a vehicle behaviour like 'brakes suddenly' or 'cuts in front of ego'."
        )
        npc_behavior = None

    # LLM clarification message (only add if no more specific error was already raised)
    if p.clarificationMessage and not messages:
        messages.append(p.clarificationMessage)

    # Missing required-field catch-all
    if not messages:
        missing: list = []
        if ego_speed is None:
            missing.append("ego speed (e.g. 50 km/h)")
        if not npc_type:
            missing.append("NPC actor type (car, truck, pedestrian, or cyclist)")
        if not npc_behavior:
            missing.append("NPC behaviour")
        if not ego_response:
            missing.append("ego response")
        if missing:
            messages.append("Please provide more details: " + ", ".join(missing) + ".")

    # Apply safe defaults for optional fields
    traffic = max(0, min(int(p.trafficVehicles or 0), 3))
    time_of_day = p.timeOfDay or "Day"
    weather = p.weather or "clear"
    npc_speed_raw = float(p.npcSpeed) if p.npcSpeed is not None else _NPC_SPEED_DEFAULTS.get(npc_type or "", 30.0)
    npc_speed = max(1.0, min(npc_speed_raw, 130.0))

    parsed_dict = {
        "egoSpeed": ego_speed,
        "trafficVehicles": traffic,
        "timeOfDay": time_of_day,
        "weather": weather,
        "npcType": npc_type,
        "npcSpeed": npc_speed,
        "npcBehavior": npc_behavior,
        "egoResponse": ego_response,
    }

    if messages:
        return None, parsed_dict, messages

    # All required fields present — return kwargs the caller uses to build ScenarioRequest.
    request_kwargs = {
        "egoSpeed": ego_speed,
        "trafficVehicles": traffic,
        "weather": weather,
        "timeOfDay": time_of_day,
        "npcType": npc_type,
        "npcBehavior": npc_behavior,
        "egoResponse": ego_response,
        "prompt": raw_text,
        "npcSpeed": npc_speed,
    }
    return request_kwargs, parsed_dict, []


def llm_parse_scenario(text: str):
    """
    Parse a natural-language scenario description using Gemini.

    Returns one of:
      (request_kwargs, parsed_dict, [])       — success; caller builds ScenarioRequest(**kwargs)
      (None, parsed_dict, [str, ...])         — Gemini ran but validation failed; show messages
      (None, None, None)                      — LLM unavailable or API error; use rule-based fallback
    """
    if not LLM_AVAILABLE or not (text or "").strip():
        return None, None, None

    try:
        client = _genai.Client(api_key=_GEMINI_KEY)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=text.strip(),
            config={
                "system_instruction": _SYSTEM,
                "response_mime_type": "application/json",
                "response_schema": ScenarioParsed,
            },
        )
        parsed_model = getattr(response, "parsed", None)
        if parsed_model is None and getattr(response, "text", None):
            parsed_model = ScenarioParsed.model_validate_json(response.text)
        if parsed_model is None:
            return None, None, None
    except Exception:
        # Any API / SDK / network failure → caller falls back to rule-based.
        return None, None, None

    return _validate(parsed_model, text)
