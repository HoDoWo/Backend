"""AR scan: name a device and its main controls in a live camera frame.

The AR tab of the app has no chat and no guide: the user points the phone at a
washing machine, an oven, a keyboard… and the app sends one camera frame here.
The vision model names the device and lists its main visible controls, each
with a short label, one sentence on what it does and its centre in the frame,
so the app can put a label on the real button.
Points are normalised to the frame (0..1 from the left and top edges).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

import config
import logging_config
from manual_retrieve import _image_data_url, _load_json_object
from panel_analyze import _language_name

_get_openrouter_llm = config.get_openrouter_llm

logger = logging_config.get_logger(__name__)

# The app shows at most this many labels: more would cover the device.
MAX_CONTROLS = 8

# Boxes come as [ymin, xmin, ymax, xmax] on a 0..1000 grid: the format vision
# models localise best (more precise than asking for a single point).
BOX_SCALE = 1000
MAX_LABEL_CHARS = 30
MAX_DESCRIPTION_CHARS = 160
MAX_NAME_CHARS = 80


class ArScanError(RuntimeError):
    """The model did not answer with usable JSON."""


class ScannedDevice(BaseModel):
    name: str
    brand: str | None = None
    model: str | None = None


class ScannedControl(BaseModel):
    label: str
    description: str = ""
    x: float
    y: float


class ArScan(BaseModel):
    device: ScannedDevice | None
    controls: list[ScannedControl]


SYSTEM_PROMPT = (
    "You look at one camera frame of a household device: a home appliance (washing machine, "
    "oven, microwave, dishwasher, fridge, hob, coffee machine…), a computer keyboard, a remote "
    "control or similar. Name the device as precisely as the frame allows (brand and model only "
    "when readable). Then list its MAIN controls that are clearly visible in the frame: the "
    "buttons, dials, switches, displays or keys a person uses most (on a keyboard: e.g. Enter, "
    "Space, Shift, Esc, Backspace, arrow keys, power). At most {max_controls}, most important "
    "first. For each: a short label (1 to 3 words), one sentence on what it does, and its "
    "bounding box tight around the control itself (not its printed caption), as "
    "[ymin, xmin, ymax, xmax] on a 0-1000 grid of the image (0,0 = top-left). Do not guess "
    "hidden or unreadable controls. LANGUAGE: write the device name, every label and every "
    "description in {language}, translating the text printed on the device when it is in "
    "another language; never mix languages. Respond ONLY with JSON: "
    '{{"device": {{"name": "...", "brand": "..." or null, "model": "..." or null}} or null, '
    '"controls": [{{"label": "...", "description": "...", "box": [ymin, xmin, ymax, xmax]}}]}}. '
    'Use {{"device": null, "controls": []}} when no device is visible.'
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _optional(value: Any, limit: int) -> str | None:
    text = _text(value, limit)
    return text if text and text.lower() not in {"null", "none", "unknown"} else None


def coerce_device(raw: Any) -> ScannedDevice | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name"), MAX_NAME_CHARS)
    if not name:
        return None
    return ScannedDevice(
        name=name,
        brand=_optional(raw.get("brand"), MAX_NAME_CHARS),
        model=_optional(raw.get("model"), MAX_NAME_CHARS),
    )


def _centre(item: dict[str, Any]) -> tuple[float | None, float | None]:
    """Centre of the control, 0..1: from its 0-1000 box, or from x/y (0..1) as a fallback."""
    box = item.get("box") or item.get("box_2d")
    if isinstance(box, list) and len(box) == 4:
        values = [_number(value) for value in box]
        if all(value is not None for value in values):
            ymin, xmin, ymax, xmax = values  # type: ignore[misc]
            if 0 <= xmin <= xmax <= BOX_SCALE and 0 <= ymin <= ymax <= BOX_SCALE:
                return (xmin + xmax) / 2 / BOX_SCALE, (ymin + ymax) / 2 / BOX_SCALE
    return _number(item.get("x")), _number(item.get("y"))


def coerce_controls(raw: Any) -> list[ScannedControl]:
    """Keep labelled, in-frame controls; one per label; at most :data:`MAX_CONTROLS`."""
    if not isinstance(raw, list):
        return []
    controls: list[ScannedControl] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), MAX_LABEL_CHARS)
        x, y = _centre(item)
        if not label or label.lower() in seen or x is None or y is None:
            continue
        if not (0 <= x <= 1 and 0 <= y <= 1):
            continue
        seen.add(label.lower())
        controls.append(
            ScannedControl(
                label=label,
                description=_text(item.get("description"), MAX_DESCRIPTION_CHARS),
                x=round(x, 4),
                y=round(y, 4),
            )
        )
        if len(controls) == MAX_CONTROLS:
            break
    return controls


def coerce_scan(payload: dict[str, Any]) -> ArScan:
    device = coerce_device(payload.get("device"))
    # Controls without a device make no sense to the user: the frame shows no device.
    controls = coerce_controls(payload.get("controls")) if device else []
    return ArScan(device=device, controls=controls)


def build_messages(frame_path: str, language: str = "en") -> list[Any]:
    prompt = SYSTEM_PROMPT.format(max_controls=MAX_CONTROLS, language=_language_name(language))
    return [
        ("system", prompt),
        (
            "human",
            [
                {"type": "text", "text": "Name this device and its main visible controls."},
                {"type": "image_url", "image_url": {"url": _image_data_url(frame_path)}},
            ],
        ),
    ]


def _as_text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def scan_frame(frame_path: str, language: str = "en") -> ArScan:
    """Device and main controls in the frame. Retries once on invalid JSON, then raises :class:`ArScanError`."""
    llm = _get_openrouter_llm()
    messages = build_messages(frame_path, language)
    for attempt in range(2):
        response = llm.invoke(messages)
        text = _as_text(response.content)
        payload = _load_json_object(text)
        if payload is not None:
            scan = coerce_scan(payload)
            logger.info(
                "AR scan: device=%s, %s controls", scan.device.name if scan.device else None, len(scan.controls)
            )
            return scan
        logger.warning("AR scan attempt %s returned no JSON", attempt + 1)
        messages = messages + [
            ("ai", text),
            ("human", "That was not valid JSON. Reply again with ONLY the JSON object described."),
        ]
    raise ArScanError("AR scan returned invalid JSON")
