"""
Optional LLM-powered parsing for the "Refine with a follow-up prompt" feature.

Provider-pluggable and fully optional:
  1. Google Gemini  — if `google-genai` is installed and GEMINI_API_KEY (or
     GOOGLE_API_KEY) is set.
  2. Anthropic Claude — if `anthropic` is installed and ANTHROPIC_API_KEY is set.
  3. Otherwise unavailable — main.py falls back to its keyword parser.

In every case the model's output is constrained to a JSON schema (the
RefinementOverrides Pydantic model), so it can only return valid UI values, and
any error returns None so refinement never breaks.
"""

import os
from typing import Literal, Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Provider/SDK detection (all optional)
# ---------------------------------------------------------------------------
try:
    from google import genai as _genai
    _GEMINI_SDK = True
except Exception:
    _GEMINI_SDK = False

try:
    import anthropic as _anthropic
    _ANTHROPIC_SDK = True
except Exception:
    _ANTHROPIC_SDK = False


def _is_real_key(value: str) -> bool:
    """A key is 'real' if present and not one of the .env placeholders."""
    return bool(value) and "REPLACE_WITH_YOUR" not in value.upper()


_GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY") or ""

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_MODEL = os.getenv("REFINE_MODEL", "claude-opus-4-8")

# Pick a provider: Gemini first (the project's key), then Anthropic.
if _GEMINI_SDK and _is_real_key(_GEMINI_KEY):
    PROVIDER = "gemini"
elif _ANTHROPIC_SDK and _is_real_key(_ANTHROPIC_KEY):
    PROVIDER = "anthropic"
else:
    PROVIDER = None

LLM_AVAILABLE = PROVIDER is not None
PROVIDER_LABEL = {"gemini": "Gemini", "anthropic": "Claude"}.get(PROVIDER, "keyword rules")


# ---------------------------------------------------------------------------
# Shared structured-output schema
# ---------------------------------------------------------------------------
class RefinementOverrides(BaseModel):
    """Each field is optional; the model sets only what the instruction implies.
    Literals constrain it to valid UI values."""

    # String enums: Gemini's structured-output schema requires enum values to be
    # strings. They are converted back to int in _only_set().
    egoSpeed: Optional[Literal["30", "50", "80", "100"]] = None
    trafficVehicles: Optional[Literal["0", "1", "2", "3"]] = None
    timeOfDay: Optional[Literal["Day", "Night"]] = None
    weather: Optional[Literal["clear", "rain", "fog", "snow"]] = None
    npcType: Optional[Literal["car", "truck", "pedestrian", "cyclist"]] = None
    npcBehavior: Optional[
        Literal[
            "brakes suddenly",
            "changes lane suddenly",
            "cuts in front of ego",
            "crosses the road",
        ]
    ] = None
    egoResponse: Optional[
        Literal[
            "brakes immediately",
            "steers to avoid",
            "slows down",
            "keeps lane and reduces speed",
        ]
    ] = None
    improve: Optional[bool] = None


# ---------------------------------------------------------------------------
# SYSTEM INSTRUCTIONS for the refinement LLM (Gemini / Claude).
#
# Structured with the standard prompt framework, one section per the diagram:
#   Role        - who the model is
#   Task        - what it must do
#   Context     - domain facts and the allowed values it must work within
#   Example     - a worked input -> output pair to anchor the behaviour
#   Format      - the exact output shape
#   Boundaries  - hard rules and what it must NOT do
#   Note        - extra hints (relative terms, synonyms)
#
# The *current* scenario values are dynamic, so they are supplied per request in
# _build_prompt() (the user turn), not here.
# ---------------------------------------------------------------------------
_SYSTEM = """\
# ROLE
You are a parameter-extraction assistant for an OpenSCENARIO driving-scenario
generator (used with the esmini simulator). You turn a short, plain-language
"refine" instruction into structured changes to the scenario's parameters.

# TASK
Given the current scenario parameters and one free-text instruction, decide
which parameters the instruction asks to change, and output only those changes.

# CONTEXT
The scenario has these parameters and allowed values (use them EXACTLY):
- egoSpeed: one of 30, 50, 80, 100 (km/h)
- trafficVehicles: one of 0, 1, 2, 3
- timeOfDay: "Day" or "Night"
- weather: "clear", "rain", "fog", or "snow"
- npcType: "car", "truck", "pedestrian", or "cyclist"
- npcBehavior: "brakes suddenly", "changes lane suddenly", "cuts in front of ego",
  or "crosses the road"
- egoResponse: "brakes immediately", "steers to avoid", "slows down", or
  "keeps lane and reduces speed"
- improve: true/false  (true = sharper, more aggressive actor behaviour)

# EXAMPLE
Instruction: "make it a foggy night with a truck that cuts in front"
Output: {"timeOfDay": "Night", "weather": "fog", "npcType": "truck",
         "npcBehavior": "cuts in front of ego"}

# FORMAT
Return ONLY the fields that should change, as JSON matching the provided schema.
Leave every unchanged field unset (null). Output JSON only - no explanation.

# BOUNDARIES
- Only change a field the instruction clearly implies; never guess.
- Never use a value outside the allowed lists above.
- Pedestrians and cyclists can ONLY "crosses the road"; cars and trucks can NEVER
  "crosses the road" - choose a valid vehicle behaviour instead.
- If nothing in the instruction maps to a parameter, change nothing.

# NOTE
- Relative speed: "faster"/"slower" moves to the next/previous value among
  30, 50, 80, 100.
- Traffic: "more"/"less"/"no traffic" adjusts the count (min 0, max 3).
- "more aggressive", "sharper", "more realistic" -> improve = true.
- Common synonyms: lorry -> truck, person/walker -> pedestrian, bike -> cyclist,
  swerve -> steers to avoid, slam the brakes -> brakes immediately.
"""


def _build_prompt(text: str, current) -> str:
    """The dynamic user turn: the current values plus the instruction to apply.
    (All rules/allowed values live in _SYSTEM above.)"""
    return (
        "Current parameters: "
        f"egoSpeed={current.egoSpeed}, trafficVehicles={current.trafficVehicles}, "
        f"timeOfDay={current.timeOfDay}, weather={current.weather}, "
        f"npcType={current.npcType}, npcBehavior={current.npcBehavior}, "
        f"egoResponse={current.egoResponse}.\n\n"
        f'Instruction: "{text}"'
    )


def _only_set(parsed: "RefinementOverrides") -> dict:
    out = {k: v for k, v in parsed.model_dump().items() if v is not None}
    # Convert the string-enum numeric fields back to int.
    for key in ("egoSpeed", "trafficVehicles"):
        if key in out:
            out[key] = int(out[key])
    return out


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------
def _gemini_parse(text: str, current) -> Optional[dict]:
    client = _genai.Client(api_key=_GEMINI_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=_build_prompt(text, current),
        config={
            "system_instruction": _SYSTEM,
            "response_mime_type": "application/json",
            "response_schema": RefinementOverrides,
        },
    )
    parsed = getattr(response, "parsed", None)
    if parsed is None and getattr(response, "text", None):
        parsed = RefinementOverrides.model_validate_json(response.text)
    return _only_set(parsed) if parsed is not None else None


def _anthropic_parse(text: str, current) -> Optional[dict]:
    client = _anthropic.Anthropic()
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(text, current)}],
        output_format=RefinementOverrides,
    )
    parsed = response.parsed_output
    return _only_set(parsed) if parsed is not None else None


def llm_parse_refinement(text: str, current) -> Optional[dict]:
    """
    Parse a refinement instruction with the active LLM provider. Returns a dict
    of overrides (only fields that should change), or None if no provider is
    available or anything goes wrong — the caller then uses the keyword parser.
    """
    if not LLM_AVAILABLE or not text.strip():
        return None
    try:
        if PROVIDER == "gemini":
            return _gemini_parse(text, current)
        if PROVIDER == "anthropic":
            return _anthropic_parse(text, current)
    except Exception:
        # Never let an API/SDK issue break refinement — fall back to keywords.
        return None
    return None
