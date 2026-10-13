"""
LangChain agent construction for the HoDoWo appliance assistant.
"""

import os
from typing import Any

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
if __package__:
    from .logging_config import get_logger
    from .identify_appliance import identify_appliance
    from .manual_retrieve import retrieve_manual
else:
    from logging_config import get_logger
    from identify_appliance import identify_appliance
    from manual_retrieve import retrieve_manual
from dotenv import load_dotenv

load_dotenv()

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are HoDoWo, a cautious multimodal home-appliance assistant.
When an image is provided, identify the appliance before retrieving instructions. If no
image is provided, answer using the user's text and suggest attaching an image so you can
identify the appliance and provide a visual guide; do not fail or guess the model.
Generate an annotated image when the user asks how to do something or when visual guidance
is useful.
If any tool reports low confidence, missing model, needs_better_image, or needs_clarification,
ask the user for the missing information or image instead of guessing. Do not retry a tool
when the required input is absent. Never recommend unsafe servicing, bypassing safety
features, or opening the appliance. End with a concise answer and include the generated
image URL when available."""


def generate_overlay(
    image_path: str,
    instructions: str,
    steps: list[str],
) -> dict[str, Any]:
    """Declare the annotated-image generation tool contract."""
    del image_path, instructions, steps
    raise NotImplementedError("image overlay generation is not implemented")


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

    def overlay_tool(instructions: str, steps: list[str]) -> dict[str, Any]:
        return generate_overlay(image_path, instructions, steps)

    identify = StructuredTool.from_function(
        func=identify_tool,
        name="appliance_identification",
        description="Identify the appliance, brand and model from the uploaded image.",
    )
    manual = StructuredTool.from_function(
        func=retrieve_manual,
        name="manual_retrieve",
        description=(
            "Retrieve safe operating instructions using the complete structured "
            "result from appliance_identification and the user's requested action. "
            "Pass the previous identification result as identification."
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
