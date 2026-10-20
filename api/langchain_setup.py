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
    from .config import get_openrouter_api_key, get_openrouter_model
    from .logging_config import get_logger
    from .image_generation import generate_overlay
    from .manual_retrieve import clarification_manual, retrieve_manual
else:
    from config import get_openrouter_api_key, get_openrouter_model
    from logging_config import get_logger
    from image_generation import generate_overlay
    from manual_retrieve import clarification_manual, retrieve_manual
from dotenv import load_dotenv

load_dotenv()

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are HoDoWo, a cautious multimodal home-appliance assistant.
When an image is provided, use manual_retrieve to identify the appliance and retrieve
instructions in one call. If no image is provided, answer using the user's text and
suggest attaching an image so you can identify the appliance and provide a visual guide;
do not fail or guess the model.
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

    manual = next(
        (item for item in reversed(results) if {"summary", "steps"}.issubset(item)),
        None,
    )

    try:
        if manual is None:
            try:
                manual = retrieve_manual(user_request, image_path)
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
    return ChatOpenAI(
        model=get_openrouter_model(),
        api_key=get_openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
    )


def build_agent(image_path: str | None = None):
    """Build the HoDoWo agent with the declared appliance tools."""
    logger.debug("Building LangChain agent (has_image=%s)", image_path is not None)

    def manual_tool(
        user_request: str,
        brand: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        try:
            return retrieve_manual(user_request, image_path, brand or None, model or None)
        except ValueError as exc:
            return clarification_manual(str(exc))

    def overlay_tool(instructions: str, steps: list[str]) -> dict[str, Any]:
        # A failed overlay (Kie not configured, out of credits, timeout...) must not
        # stop the whole answer: the agent still returns the guide without the image.
        try:
            return generate_overlay(image_path, instructions, steps)
        except (FileNotFoundError, RuntimeError, ValueError, KeyError) as exc:
            logger.warning("Image overlay failed: %s", exc)
            return {"image_url": None, "error": f"The annotated image is not available: {exc}"}

    manual = StructuredTool.from_function(
        func=manual_tool,
        name="manual_retrieve",
        description=(
            "Use the original image and user request to identify the appliance, brand "
            "and model, then retrieve safe operating instructions. Optional brand and "
            "model hints may be provided. The original image is automatically supplied."
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

    def panel_tool(
        brand: str = "",
        model: str = "",
        language: str = "en",
    ) -> dict[str, Any]:
        """Detect the controls on the panel in the uploaded photo."""
        if not image_path:
            return {"controls": [], "error": "no panel image was uploaded"}

        # Imported here on purpose: the module-level import block is also edited
        # by the in-flight image-generation work (#10), so keeping it untouched
        # avoids a merge conflict.
        if __package__:
            from .panel_analyze import PanelControlError, analyze_panel
        else:
            from panel_analyze import PanelControlError, analyze_panel

        try:
            controls = analyze_panel(
                image_path, brand=brand, model=model, language=language
            )
        except (PanelControlError, FileNotFoundError) as exc:
            logger.warning("Control panel analysis failed: %s", exc)
            return {"controls": [], "error": str(exc)}
        return {"controls": [control.model_dump(mode="json") for control in controls]}

    panel = StructuredTool.from_function(
        func=panel_tool,
        name="control_panel_analysis",
        description=(
            "Detect the buttons, dials, switches and displays on the appliance "
            "control panel in the uploaded photo. Returns one normalised bounding "
            "box (0..1) per control, ready to be highlighted on the image."
        ),
    )

    return create_agent(
        _get_openrouter_llm(),
        [manual, overlay, panel],
        system_prompt=SYSTEM_PROMPT,
    )
