"""Manual retrieval tool implementation."""

from __future__ import annotations

import base64
import json
import mimetypes
import pathlib
from typing import Any

import config
import logging_config

from api import config, logging_config

_get_openrouter_llm = config.config.get_openrouter_llm


logger = logging_config.logging_config.get_logger(__name__)

REQUIRED_MANUAL_FIELDS = {
    "summary",
    "steps",
    "warnings",
    "needs_clarification",
    "confidence",
}



def _image_data_url(image_path: str) -> str:
    path = pathlib.Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Appliance image does not exist: {image_path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _parse_response(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    if not isinstance(content, str):
        raise RuntimeError("Manual retrieval returned a non-text response")

    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Manual retrieval returned invalid JSON") from exc

    if not isinstance(result, dict) or not REQUIRED_MANUAL_FIELDS.issubset(result):
        raise RuntimeError("Manual retrieval response is missing required fields")

    if not isinstance(result["steps"], list) or not all(
        isinstance(step, str) for step in result["steps"]
    ):
        raise RuntimeError("Manual retrieval steps must be a list of strings")
    if not isinstance(result["warnings"], list) or not all(
        isinstance(warning, str) for warning in result["warnings"]
    ):
        raise RuntimeError("Manual retrieval warnings must be a list of strings")
    if not isinstance(result["needs_clarification"], bool):
        raise RuntimeError("Manual retrieval needs_clarification must be boolean")
    confidence = result["confidence"]
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise RuntimeError("Manual retrieval confidence must be between 0 and 1")

    return result


def retrieve_manual(
    user_request: str,
    image_path: str | None = None,
    brand: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Identify the appliance and retrieve safe guidance in one multimodal call."""
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError("user_request must not be empty")

    logger.debug(
        "Retrieving manual guidance with optional hints brand=%s model=%s",
        brand,
        model,
    )
    hints = {
        "brand": brand or None,
        "model": model or None,
    }
    human_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"User request:\n{user_request}\n\n"
                "Optional appliance hints (verify these against the image; do not "
                f"treat them as authoritative):\n{json.dumps(hints, ensure_ascii=True)}"
            ),
        }
    ]
    if image_path:
        human_content.append(
            {"type": "image_url", "image_url": {"url": _image_data_url(image_path)}}
        )

    response = _get_openrouter_llm().invoke(
        [
            (
                "system",
                (
                    "You are a home-appliance manual retrieval tool. Use the supplied "
                    "user request and original appliance image to first identify the "
                    "appliance, brand and model, then answer the requested operation "
                    "with safe, model-specific instructions. Optional brand/model "
                    "hints are only clues and must be verified against the image. "
                    "Do not invent button names or model details. If the image is "
                    "unclear, the model cannot be identified, or the request cannot "
                    "be answered reliably, set needs_clarification=true and explain "
                    "what is missing. Never advise opening the appliance, bypassing "
                    "safety features, or unsafe repairs. Respond ONLY with JSON "
                    "containing appliance_name, brand, model, identification_confidence, "
                    "visible_evidence, needs_better_image, summary, steps (array of "
                    "strings), warnings (array of strings), needs_clarification "
                    "(boolean), and confidence (number from 0 to 1). Use null for "
                    "unknown appliance fields."
                ),
            ),
            (
                "human",
                human_content,
            ),
        ]
    )
    result = _parse_response(response.content)
    logger.info(f"Manual retrieval completed successfully: {result}")
    return result


def clarification_manual(reason: str) -> dict[str, Any]:
    """Build safe fallback guidance when an agent supplies incomplete context."""
    logger.warning("Manual retrieval requires clarification: %s", reason)
    return {
        "summary": reason,
        "steps": [
            "Inspect the original appliance image and identify the relevant control.",
            "Ask the user to confirm the control or provide the brand and model.",
        ],
        "warnings": [
            "Do not press or turn an unlabeled control until the appliance model is confirmed."
        ],
        "needs_clarification": True,
        "confidence": 0,
    }
