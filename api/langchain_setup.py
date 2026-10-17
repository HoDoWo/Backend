"""
LangChain agent construction for the HoDoWo appliance assistant.
"""

import os
import json
from typing import Any

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
if __package__:
    from .logging_config import get_logger
    from .identify_appliance import identify_appliance
    from .image_generation import generate_overlay
    from .manual_retrieve import clarification_manual, retrieve_manual
else:
    from logging_config import get_logger
    from identify_appliance import identify_appliance
    from image_generation import generate_overlay
    from manual_retrieve import clarification_manual, retrieve_manual
from dotenv import load_dotenv

load_dotenv()

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are HoDoWo, a cautious multimodal home-appliance assistant.
When an image is provided, identify the appliance before retrieving instructions. If no
image is provided, answer using the user's text and suggest attaching an image so you can
identify the appliance and provide a visual guide; do not fail or guess the model.
Generate an annotated image when the user asks how to do something or when visual guidance
is useful. When an image is available, always call image_generation after manual_retrieve,
even when manual_retrieve needs clarification; use the safest available steps or a visual
request for the user to identify the missing control. Provide super summarized instructions and steps to the generate image tool.
If any tool reports low confidence, missing model, needs_better_image, or needs_clarification,
ask the user for the missing information or image instead of guessing. Do not retry a tool
when the required input is absent. Never recommend unsafe servicing, bypassing safety
features, or opening the appliance. If user provides an image, always try to finish your answer with an generated image overlay, even if the manual retrieval needs clarification"""


def _json_tool_results(messages: list[object]) -> list[dict[str, Any]]:
    """Return JSON objects emitted by LangChain tool messages."""
    results: list[dict[str, Any]] = []
    for message in messages:
        content = getattr(message, "content", None)
        candidates = content if isinstance(content, list) else [content]
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                results.append(value)
    return results


def ensure_image_overlay(
    messages: list[object],
    image_path: str | None,
    user_request: str,
) -> str | None:
    """Complete the image workflow if the agent stopped before image generation."""
    if not image_path:
        return None

    results = _json_tool_results(messages)
    image_result = next((item for item in reversed(results) if item.get("image_url")), None)
    if image_result:
        return str(image_result["image_url"])

    identification = next(
        (item for item in reversed(results) if "appliance_name" in item),
        None,
    )
    manual = next(
        (item for item in reversed(results) if {"summary", "steps"}.issubset(item)),
        None,
    )

    try:
        if identification is None:
            identification = identify_appliance(image_path, user_request)
        if manual is None:
            try:
                manual = retrieve_manual(identification, user_request, image_path)
            except ValueError as exc:
                manual = clarification_manual(str(exc))

        steps = manual.get("steps") or [
            "Follow the requested operation using the controls visible in the original image."
        ]
        overlay = generate_overlay(
            image_path,
            str(manual.get("summary") or user_request),
            steps,
        )
        return str(overlay["image_url"])
    except (FileNotFoundError, RuntimeError, ValueError, KeyError) as exc:
        logger.warning("Could not complete the image overlay workflow: %s", exc)
        return None


def _get_openrouter_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OPENROUTER_API_KEY is not configured")
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    return ChatOpenAI(
        model="openrouter/free",
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )


def build_agent(image_path: str | None = None):
    """Build the HoDoWo agent with the declared appliance tools."""
    logger.debug("Building LangChain agent (has_image=%s)", image_path is not None)

    def identify_tool(user_context: str = "") -> dict[str, Any]:
        return identify_appliance(image_path, user_context)

    def manual_tool(
        identification: dict[str, Any],
        user_request: str,
    ) -> dict[str, Any]:
        try:
            return retrieve_manual(identification, user_request, image_path)
        except ValueError as exc:
            return clarification_manual(str(exc))

    def overlay_tool(instructions: str, steps: list[str]) -> dict[str, Any]:
        return generate_overlay(image_path, instructions, steps)

    identify = StructuredTool.from_function(
        func=identify_tool,
        name="appliance_identification",
        description="Identify the appliance, brand and model from the uploaded image.",
    )
    manual = StructuredTool.from_function(
        func=manual_tool,
        name="manual_retrieve",
        description=(
            "Retrieve safe operating instructions using the complete structured "
            "result from appliance_identification and the user's requested action. "
            "Pass the previous identification result as identification. The original "
            "image is automatically supplied for additional context."
        ),
    )
    overlay = StructuredTool.from_function(
        func=overlay_tool,
        name="image_generation",
        description=(
            "Generate an annotated overlay of the original image using "
            "Kie Nano Banana."
        ),
    )

    return create_agent(
        _get_openrouter_llm(),
        [identify, manual, overlay],
        system_prompt=SYSTEM_PROMPT,
    )
