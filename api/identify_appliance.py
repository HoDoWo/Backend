"""Appliance identification tool implementation."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

if __package__:
    from .logging_config import get_logger
else:
    from logging_config import get_logger


logger = get_logger(__name__)

REQUIRED_FIELDS = {
    "appliance_name",
    "brand",
    "model",
    "confidence",
    "visible_evidence",
    "needs_better_image",
}


def _get_openrouter_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    return ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0,
    )


def _image_data_url(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Appliance image does not exist: {image_path}")

    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded_image = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded_image}"


def _parse_response(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    if not isinstance(content, str):
        raise RuntimeError("Appliance identification returned a non-text response")

    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        if text.endswith("```"):
            text = text[:-3].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Appliance identification returned invalid JSON"
        ) from exc

    if not isinstance(result, dict) or not REQUIRED_FIELDS.issubset(result):
        raise RuntimeError(
            "Appliance identification response is missing required fields"
        )

    confidence = result["confidence"]
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise RuntimeError("Appliance identification confidence must be between 0 and 1")

    if not isinstance(result["needs_better_image"], bool):
        raise RuntimeError("Appliance identification needs_better_image must be boolean")

    return result


def identify_appliance(image_path: str | None, user_context: str = "") -> dict[str, Any]:
    """Identify an appliance, brand, model, evidence and confidence from an image."""
    if not image_path:
        logger.info("Appliance identification skipped because no image was provided")
        return {
            "appliance_name": None,
            "brand": None,
            "model": None,
            "confidence": 0,
            "visible_evidence": (
                "No appliance image was provided. Answer using the user's text "
                "and suggest attaching an image for a visual guide."
            ),
            "needs_better_image": True,
        }

    image_url = _image_data_url(image_path)
    logger.debug("Sending appliance image to the identification model")

    response = _get_openrouter_llm().invoke(
        [
            (
                "system",
                (
                    "You are an image analysis tool. Identify the home appliance "
                    "in the image. Respond ONLY with a valid JSON object. "
                    "Required keys: appliance_name, brand, model, confidence "
                    "(number from 0 to 1), visible_evidence, needs_better_image "
                    "(boolean). Use null when a value cannot be identified."
                ),
            ),
            (
                "human",
                [
                    {
                        "type": "text",
                        "text": user_context or "Identify this appliance.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            ),
        ]
    )
    logger.info(f"Appliance identification completed successfully: {response.content}")
    result = _parse_response(response.content)
    return result
