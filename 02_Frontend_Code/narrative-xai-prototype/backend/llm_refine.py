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


_SYSTEM = (
    "You convert a short free-text instruction into parameter overrides for a "
    "driving-scenario generator. Only set a field if the instruction clearly "
    "implies changing it; otherwise leave it null. Use exactly the allowed values."
)


def _build_prompt(text: str, current) -> str:
    return (
        f"Current parameters: egoSpeed={current.egoSpeed} km/h, "
        f"trafficVehicles={current.trafficVehicles}, timeOfDay={current.timeOfDay}, "
        f"weather={current.weather}, npcType={current.npcType}, "
        f"npcBehavior={current.npcBehavior}, egoResponse={current.egoResponse}.\n\n"
        f'Instruction: "{text}"\n\n'
        "Return only the fields that should change. For 'faster'/'slower', pick the "
        "next/previous speed among 30, 50, 80, 100. 'more'/'less'/'no traffic' adjusts "
        "the traffic count. 'more aggressive' or 'sharper' sets improve=true. Pedestrians "
        "and cyclists can only cross the road; cars and trucks cannot cross."
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
