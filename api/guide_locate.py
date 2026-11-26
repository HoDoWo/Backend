"""Find the controls of guide steps in a live camera frame (AR auto-alignment).

The AR view of the app pins every step of a repair guide on the real appliance
once it knows where two controls are. Instead of asking the user to aim at
them, the app sends one camera frame and the steps to look for; this module
asks the vision model where each control is in that frame.

The optional reference photo is the photo the guide was made from, with the
pin of each step on it, so the model can match "this button" between the two.
Points are normalised to the frame (0..1 from the left and top edges).
"""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict

import config
import logging_config
from manual_retrieve import _image_data_url, _load_json_object

_get_vision_llm = config.get_ar_vision_llm

logger = logging_config.get_logger(__name__)

# Bounds on what a client may send, so one request cannot grow the prompt without limit.
MAX_STEPS = 30
MAX_TARGETS = 4
MAX_TEXT_CHARS = 300

# A point the model is less sure about than this is left out: the app then asks the user.
MIN_CONFIDENCE = 0.5


class GuideLocateError(RuntimeError):
    """The model did not answer with usable JSON."""


class GuideStep(BaseModel):
    """One step of the guide, as the app sends it."""

    model_config = ConfigDict(extra="ignore")

    step: int
    title: str = ""
    description: str = ""
    # Where the step's control is on the reference photo, when the guide has a pin for it.
    pin_x: float | None = None
    pin_y: float | None = None


class StepPoint(BaseModel):
    """Where a step's control is in the camera frame."""

    step: int
    x: float
    y: float
    confidence: float


SYSTEM_PROMPT = (
    "You locate the controls of home-appliance repair steps in a live camera frame. "
    "The FIRST image is the camera frame. A SECOND image, when present, is the reference "
    "photo the guide was made from; for some steps you get the point of their control on "
    "that reference photo (0..1 from its left and top edges). Find the SAME physical "
    "control (button, dial, display, label, door handle, filter cover) in the camera frame. "
    "Only answer for the requested steps. Do not guess: if the control is not clearly "
    "visible in the camera frame, leave that step out. Respond ONLY with JSON: "
    '{"points": [{"step": <step number>, "x": 0..1 from the left edge of the camera frame, '
    '"y": 0..1 from the top edge, "confidence": 0..1}]} with the point at the centre of '
    'the control. Use {"points": []} when none is visible.'
)


def _clip(text: str) -> str:
    return text.strip()[:MAX_TEXT_CHARS]


def parse_steps(raw: str) -> list[GuideStep]:
    """The ``steps`` form field: a JSON array of steps. Raises ValueError when unusable."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("steps must be a JSON array") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("steps must be a non-empty JSON array")
    if len(payload) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    steps = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("every step must be an object")
        step = GuideStep.model_validate(item)
        steps.append(step.model_copy(update={"title": _clip(step.title), "description": _clip(step.description)}))
    return steps


def parse_targets(raw: str, steps: list[GuideStep]) -> list[int]:
    """The ``targets`` form field: step numbers to find, e.g. "1,3". Empty means all steps."""
    known = [step.step for step in steps]
    if not raw.strip():
        targets = known
    else:
        try:
            targets = [int(part) for part in raw.split(",") if part.strip()]
        except ValueError as exc:
            raise ValueError("targets must be step numbers separated by commas") from exc
    unique = list(dict.fromkeys(target for target in targets if target in known))
    if not unique:
        raise ValueError("targets must name steps of the guide")
    return unique[:MAX_TARGETS]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _step_number(value: Any) -> int | None:
    """A step number given as 3, 3.0 or "3"; None for anything else (booleans included)."""
    if isinstance(value, bool):
        return None
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def coerce_points(raw: Any, targets: list[int]) -> list[StepPoint]:
    """Keep one confident, in-frame point per requested step; drop everything else."""
    if isinstance(raw, dict):
        raw = raw.get("points")
    if not isinstance(raw, list):
        return []
    points: dict[int, StepPoint] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        step = _step_number(item.get("step"))
        x, y = _number(item.get("x")), _number(item.get("y"))
        confidence = _number(item.get("confidence"))
        if step is None or step not in targets or step in points:
            continue
        if x is None or y is None or not (0 <= x <= 1 and 0 <= y <= 1):
            continue
        confidence = 1.0 if confidence is None else min(1.0, max(0.0, confidence))
        if confidence < MIN_CONFIDENCE:
            continue
        points[step] = StepPoint(step=step, x=round(x, 4), y=round(y, 4), confidence=round(confidence, 2))
    return [points[target] for target in targets if target in points]


def _describe(step: GuideStep) -> str:
    line = f"Step {step.step}: {step.title}"
    if step.description:
        line += f" - {step.description}"
    if step.pin_x is not None and step.pin_y is not None:
        line += f" (on the reference photo at x={step.pin_x:.3f}, y={step.pin_y:.3f})"
    return line


def build_messages(
    frame_path: str,
    steps: list[GuideStep],
    targets: list[int],
    reference_path: str | None = None,
    appliance: str = "",
) -> list[Any]:
    lines = []
    if appliance:
        lines.append(f"Appliance: {_clip(appliance)}")
    lines.append("Guide steps:")
    lines.extend(_describe(step) for step in steps)
    lines.append("Locate in the camera frame the controls of steps: " + ", ".join(map(str, targets)))
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "\n".join(lines)},
        {"type": "image_url", "image_url": {"url": _image_data_url(frame_path)}},
    ]
    if reference_path:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(reference_path)}})
    return [("system", SYSTEM_PROMPT), ("human", content)]


def _as_text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def locate_steps(
    frame_path: str,
    steps: list[GuideStep],
    targets: list[int],
    reference_path: str | None = None,
    appliance: str = "",
) -> list[StepPoint]:
    """Points of the ``targets`` steps found in the frame (possibly none).

    Retries once when the model does not answer with JSON, then raises
    :class:`GuideLocateError`.
    """
    llm = _get_vision_llm()
    messages = build_messages(frame_path, steps, targets, reference_path, appliance)
    for attempt in range(2):
        response = llm.invoke(messages)
        text = _as_text(response.content)
        payload = _load_json_object(text)
        if payload is not None:
            points = coerce_points(payload, targets)
            logger.info("Guide locate found %s of %s steps", len(points), len(targets))
            return points
        logger.warning("Guide locate attempt %s returned no JSON", attempt + 1)
        messages = messages + [
            ("ai", text),
            ("human", "That was not valid JSON. Reply again with ONLY the JSON object described."),
        ]
    raise GuideLocateError("guide locate returned invalid JSON")
