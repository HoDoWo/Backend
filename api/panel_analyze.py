"""Computer vision for appliance control panels.

:func:`locate_panel` finds the control panel inside a full photo (S1-16);
:func:`analyze_panel` then lists the buttons and dials of that cropped panel.

The list returned here (see :class:`Control`) is the format shared with the
overlay layer (S1-15):

    {
      "controls": [
        {"id": "c1", "label": "Start/Pause", "type": "button",
         "description": "Starts or pauses the selected program",
         "box": {"x": 0.62, "y": 0.40, "w": 0.10, "h": 0.12}}
      ]
    }

Coordinates are normalised to the image size (0..1), so the client can draw
them at any resolution.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
from enum import Enum
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

if __package__:
    from .config import get_openrouter_api_key, get_openrouter_model
    from .logging_config import get_logger
else:
    from config import get_openrouter_api_key, get_openrouter_model
    from logging_config import get_logger


logger = get_logger(__name__)

# Safety cap so a broken answer cannot return an unbounded list.
MAX_CONTROLS = 40

# A located panel smaller than this fraction of the photo is ignored, so a
# stray dot cannot crop the image down to nothing. The client then falls back
# to the full photo.
MIN_PANEL_AREA = 0.01


class ControlType(str, Enum):
    """Kind of physical control on the panel."""

    button = "button"
    dial = "dial"
    switch = "switch"
    display = "display"
    other = "other"


class PanelControlError(RuntimeError):
    """Raised when the vision model cannot return a usable control list."""


class ControlBox(BaseModel):
    """Normalised bounding box of a control (0..1 relative to the image)."""

    model_config = ConfigDict(extra="ignore")

    x: float
    y: float
    w: float
    h: float


class Control(BaseModel):
    """A single control detected on the panel."""

    model_config = ConfigDict(extra="ignore")

    id: str
    label: str
    type: ControlType
    description: str = ""
    box: ControlBox


SYSTEM_PROMPT = """You are a computer-vision tool that detects the physical controls of a home
appliance control panel. Look at the cropped panel photo and list every button, dial, switch,
display and other control you can see.
Respond ONLY with a valid JSON object, no prose and no markdown, using exactly this schema:
{"controls": [{"id": "c1", "label": "<short name>", "type": "button|dial|switch|display|other", \
"description": "<one short sentence>", \
"box": {"x": <0..1>, "y": <0..1>, "w": <0..1>, "h": <0..1>}}]}
"box" is a bounding box normalised to the image size: x,y is the top-left corner and w,h the \
width and height, all between 0 and 1 (0,0 is the top-left of the image).
Write "label" and "description" in the requested language.
If the image is not a control panel, or no control is visible, respond with {"controls": []}."""

LOCATOR_SYSTEM_PROMPT = """You are a computer-vision tool that locates the control panel of a home
appliance in a photo. Look at the whole photo and find the region that holds the buttons, dials,
switches or display the user operates.
Respond ONLY with a valid JSON object, no prose and no markdown, using exactly this schema:
{"box": {"x": <0..1>, "y": <0..1>, "w": <0..1>, "h": <0..1>}}
"box" is the bounding box of the control panel normalised to the image size: x,y is the top-left \
corner and w,h the width and height, all between 0 and 1 (0,0 is the top-left of the image).
Include the display and every button, dial or switch in the box, and nothing else.
If the photo does not show a control panel, respond with {"box": null}."""

_LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "ca": "Catalan",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}


def _get_openrouter_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=get_openrouter_model(),
        api_key=get_openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
    )


def _image_data_url(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Panel image does not exist: {image_path}")

    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded_image = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded_image}"


def _language_name(language: str) -> str:
    code = (language or "").strip()
    primary = code.lower().split("-", 1)[0].split("_", 1)[0]
    return _LANGUAGE_NAMES.get(primary, code or "English")


def _as_text(content: Any) -> str:
    """Best-effort flattening of a LangChain message content into text."""
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def _load_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise PanelControlError(
                    "panel analysis returned invalid JSON"
                ) from exc
        raise PanelControlError("panel analysis returned invalid JSON") from None


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _coerce_box(raw: Any) -> ControlBox | None:
    """Build a valid, clamped box, or return None if the box is unusable."""
    if not isinstance(raw, dict):
        return None
    if not {"x", "y", "w", "h"}.issubset(raw):
        return None

    x = _clamp01(raw["x"])
    y = _clamp01(raw["y"])
    w = _clamp01(raw["w"])
    h = _clamp01(raw["h"])
    if w <= 0 or h <= 0:
        return None

    # Keep the whole box inside the image by nudging its origin back in range.
    x = min(x, 1.0 - w)
    y = min(y, 1.0 - h)

    return ControlBox(x=x, y=y, w=w, h=h)


def _coerce_type(raw: Any) -> ControlType:
    try:
        return ControlType(str(raw).strip().lower())
    except ValueError:
        return ControlType.other


def coerce_controls(raw: Any) -> list[Control]:
    """Turn a raw model value into a validated, clamped list of controls.

    Items without a label or with an unusable box are dropped. The ids are
    renumbered to ``c1..cN`` so the overlay can rely on stable identifiers.
    """
    if not isinstance(raw, list):
        return []

    controls: list[Control] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        label = str(item.get("label") or "").strip()
        box = _coerce_box(item.get("box"))
        if not label or box is None:
            continue

        controls.append(
            Control(
                id=f"c{len(controls) + 1}",
                label=label,
                type=_coerce_type(item.get("type")),
                description=str(item.get("description") or "").strip(),
                box=box,
            )
        )
        if len(controls) >= MAX_CONTROLS:
            break

    return controls


def _parse_controls(content: Any) -> list[Control]:
    text = _as_text(content)
    if not text.strip():
        raise PanelControlError("panel analysis returned an empty response")

    payload = _load_json(text)
    if isinstance(payload, dict):
        raw = payload.get("controls")
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = None

    return coerce_controls(raw)


def _build_messages(
    image_path: str,
    brand: str,
    model: str,
    language: str,
) -> list[Any]:
    lines = ["Detect the controls on this appliance control panel."]
    if brand:
        lines.append(f"Appliance brand: {brand}")
    if model:
        lines.append(f"Appliance model: {model}")
    lines.append(f"Write labels and descriptions in {_language_name(language)}.")
    context = "\n".join(lines)

    return [
        ("system", SYSTEM_PROMPT),
        (
            "human",
            [
                {"type": "text", "text": context},
                {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
            ],
        ),
    ]


def analyze_panel(
    image_path: str | None,
    brand: str = "",
    model: str = "",
    language: str = "en",
) -> list[Control]:
    """Detect the controls in the cropped panel photo.

    Retries once when the model returns malformed JSON, then raises
    :class:`PanelControlError`. An image that is not a panel yields ``[]``.
    """
    if not image_path:
        return []

    llm = _get_openrouter_llm()
    messages = _build_messages(image_path, brand, model, language)
    last_error: PanelControlError | None = None

    for attempt in range(2):
        response = llm.invoke(messages)
        try:
            return _parse_controls(response.content)
        except PanelControlError as exc:
            last_error = exc
            logger.warning(
                "Panel analysis attempt %s produced an invalid answer: %s",
                attempt + 1,
                exc,
            )
            messages = messages + [
                ("ai", _as_text(response.content)),
                (
                    "human",
                    "That was not valid JSON. Reply again with ONLY the JSON "
                    "object described, without markdown or extra text.",
                ),
            ]

    raise last_error or PanelControlError("panel analysis failed")


# --- panel location (S1-16) -----------------------------------------------


def coerce_panel_box(raw: Any) -> ControlBox | None:
    """Validate a located panel box, or return None when it is unusable.

    A box that is missing, degenerate or smaller than :data:`MIN_PANEL_AREA`
    counts as "no panel found", so the caller can fall back to the full photo.
    """
    box = _coerce_box(raw)
    if box is None:
        return None
    if box.w * box.h < MIN_PANEL_AREA:
        return None
    return box


def _parse_panel_box(content: Any) -> ControlBox | None:
    """Turn the model answer into a box, or None when it sees no panel."""
    text = _as_text(content)
    if not text.strip():
        raise PanelControlError("panel location returned an empty response")

    payload = _load_json(text)
    if isinstance(payload, dict):
        if "box" in payload:
            raw = payload["box"]
        elif "panel" in payload:
            raw = payload["panel"]
        elif {"x", "y", "w", "h"}.issubset(payload):
            # The model answered with the box itself instead of wrapping it.
            raw = payload
        else:
            raise PanelControlError("panel location returned no box")
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        raw = payload[0]
    else:
        raise PanelControlError("panel location returned no box")

    return coerce_panel_box(raw)


def _build_locator_messages(image_path: str, brand: str, model: str) -> list[Any]:
    lines = ["Locate the control panel of the appliance in this photo."]
    if brand:
        lines.append(f"Appliance brand: {brand}")
    if model:
        lines.append(f"Appliance model: {model}")
    context = "\n".join(lines)

    return [
        ("system", LOCATOR_SYSTEM_PROMPT),
        (
            "human",
            [
                {"type": "text", "text": context},
                {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}},
            ],
        ),
    ]


def locate_panel(
    image_path: str | None,
    brand: str = "",
    model: str = "",
) -> ControlBox | None:
    """Locate the control panel in a full appliance photo (S1-16).

    Returns the normalised box, or ``None`` when the photo has no panel.
    Retries once when the model returns malformed JSON, then raises
    :class:`PanelControlError`.
    """
    if not image_path:
        return None

    llm = _get_openrouter_llm()
    messages = _build_locator_messages(image_path, brand, model)
    last_error: PanelControlError | None = None

    for attempt in range(2):
        response = llm.invoke(messages)
        try:
            return _parse_panel_box(response.content)
        except PanelControlError as exc:
            last_error = exc
            logger.warning(
                "Panel location attempt %s produced an invalid answer: %s",
                attempt + 1,
                exc,
            )
            messages = messages + [
                ("ai", _as_text(response.content)),
                (
                    "human",
                    "That was not valid JSON. Reply again with ONLY the JSON "
                    "object described, without markdown or extra text.",
                ),
            ]

    raise last_error or PanelControlError("panel location failed")
