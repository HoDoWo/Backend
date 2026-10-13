"""Manual retrieval tool implementation."""

from __future__ import annotations

import json
import os
from typing import Any

from langchain_openai import ChatOpenAI

if __package__:
    from .logging_config import get_logger
else:
    from logging_config import get_logger


logger = get_logger(__name__)

REQUIRED_IDENTIFICATION_FIELDS = {
    "appliance_name",
    "brand",
    "model",
    "confidence",
    "visible_evidence",
    "needs_better_image",
}
REQUIRED_MANUAL_FIELDS = {
    "summary",
    "steps",
    "warnings",
    "needs_clarification",
    "confidence",
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
    identification: dict[str, Any],
    user_request: str,
) -> dict[str, Any]:
    """Retrieve safe operating guidance using the identification result."""
    if not isinstance(identification, dict):
        raise ValueError("identification must be an object returned by appliance identification")
    if not REQUIRED_IDENTIFICATION_FIELDS.issubset(identification):
        raise ValueError("identification is missing required appliance fields")
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError("user_request must not be empty")

    logger.debug(
        "Retrieving manual guidance for %s %s %s",
        identification.get("brand"),
        identification.get("appliance_name"),
        identification.get("model"),
    )
    response = _get_openrouter_llm().invoke(
        [
            (
                "system",
                (
                    "You are a home-appliance manual retrieval tool. Use the supplied "
                    "appliance identification result as the source of truth for the "
                    "device name, brand, model, visible evidence and confidence. "
                    "Answer the user's requested operation with safe, model-specific "
                    "instructions. Do not invent button names or model details. If the "
                    "model is null, confidence is below 0.7, needs_better_image is true, "
                    "or the request cannot be answered reliably, set "
                    "needs_clarification=true and explain what is missing. Never advise "
                    "opening the appliance, bypassing safety features, or unsafe repairs. "
                    "Respond ONLY with JSON containing: summary (string), steps "
                    "(array of strings), warnings (array of strings), "
                    "needs_clarification (boolean), and confidence (number from 0 to 1)."
                ),
            ),
            (
                "human",
                (
                    "Appliance identification result:\n"
                    f"{json.dumps(identification, ensure_ascii=True)}\n\n"
                    f"User request:\n{user_request}"
                ),
            ),
        ]
    )
    result = _parse_response(response.content)
    logger.info("Manual retrieval completed successfully")
    return result
