"""Tests for the LangChain agent tools exposed by ``build_agent``.

The LLM and the agent factory are stubbed out, so these tests never reach
OpenRouter.
"""

from typing import Any

import pytest

import langchain_setup
from panel_analyze import Control, ControlBox, ControlType, PanelControlError

EXPECTED_TOOLS = {
    "manual_retrieve",
    "image_generation",
    "control_panel_analysis",
}


def _agent_tools(monkeypatch, image_path: str | None = "panel.jpg") -> dict[str, Any]:
    """Build the agent with a fake LLM and return its tools keyed by name."""
    captured: dict[str, Any] = {}

    def fake_create_agent(llm: Any, tools: list[Any], system_prompt: str) -> object:
        captured["tools"] = tools
        return object()

    monkeypatch.setattr(langchain_setup, "create_agent", fake_create_agent)
    monkeypatch.setattr(langchain_setup, "_get_openrouter_llm", lambda: object())

    langchain_setup.build_agent(image_path)

    return {tool.name: tool for tool in captured["tools"]}


def _sample_control() -> Control:
    return Control(
        id="c1",
        label="Start/Pause",
        type=ControlType.button,
        description="Starts or pauses the program",
        box=ControlBox(x=0.62, y=0.40, w=0.10, h=0.12),
    )


def test_build_agent_exposes_the_control_panel_tool(monkeypatch):
    tools = _agent_tools(monkeypatch)

    assert set(tools) == EXPECTED_TOOLS


def test_control_panel_tool_returns_normalised_controls(monkeypatch):
    def fake_analyze_panel(
        image_path: str,
        brand: str = "",
        model: str = "",
        language: str = "en",
    ) -> list[Control]:
        assert image_path == "panel.jpg"
        assert (brand, model, language) == ("Bosch", "WAT", "es")
        return [_sample_control()]

    monkeypatch.setattr("panel_analyze.analyze_panel", fake_analyze_panel)
    tool = _agent_tools(monkeypatch)["control_panel_analysis"]

    result = tool.func(brand="Bosch", model="WAT", language="es")

    assert result == {
        "controls": [
            {
                "id": "c1",
                "label": "Start/Pause",
                "type": "button",
                "description": "Starts or pauses the program",
                "box": {"x": 0.62, "y": 0.40, "w": 0.10, "h": 0.12},
            }
        ]
    }


def test_control_panel_tool_without_an_image_returns_an_error(monkeypatch):
    called = False

    def fake_analyze_panel(*args: Any, **kwargs: Any) -> list[Control]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("panel_analyze.analyze_panel", fake_analyze_panel)
    tool = _agent_tools(monkeypatch, image_path=None)["control_panel_analysis"]

    result = tool.func()

    assert result["controls"] == []
    assert "error" in result
    assert called is False


def test_control_panel_tool_degrades_when_analysis_fails(monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> list[Control]:
        raise PanelControlError("the model returned an unusable control list")

    monkeypatch.setattr("panel_analyze.analyze_panel", boom)
    tool = _agent_tools(monkeypatch)["control_panel_analysis"]

    result = tool.func()

    assert result == {
        "controls": [],
        "error": "the model returned an unusable control list",
    }


def test_control_panel_tool_has_a_useful_description(monkeypatch):
    tool = _agent_tools(monkeypatch)["control_panel_analysis"]

    assert "control panel" in tool.description.lower()


def test_a_failed_overlay_does_not_stop_the_answer(monkeypatch):
    def broken_overlay(image_path: str, instructions: str, steps: list[str]) -> dict[str, Any]:
        raise RuntimeError("KIE_API_KEY is not configured")

    monkeypatch.setattr(langchain_setup, "generate_overlay", broken_overlay)
    tool = _agent_tools(monkeypatch)["image_generation"]

    result = tool.func(instructions="Set 180 °C with the fan", steps=["Turn the knob"])

    assert result["image_url"] is None
    assert "KIE_API_KEY is not configured" in result["error"]


def test_a_working_overlay_is_returned_unchanged(monkeypatch):
    monkeypatch.setattr(
        langchain_setup,
        "generate_overlay",
        lambda image_path, instructions, steps: {"image_url": "https://example.test/o.png"},
    )
    tool = _agent_tools(monkeypatch)["image_generation"]

    assert tool.func(instructions="x", steps=["y"]) == {"image_url": "https://example.test/o.png"}


def test_an_unreadable_manual_does_not_stop_the_answer(monkeypatch):
    def broken_manual(*args, **kwargs):
        raise RuntimeError("Manual retrieval returned invalid JSON")

    monkeypatch.setattr(langchain_setup, "retrieve_manual", broken_manual)
    tool = _agent_tools(monkeypatch)["manual_retrieve"]

    result = tool.func(user_request="How do I set 180 °C?")

    assert result["needs_clarification"] is True
    assert result["steps"]
