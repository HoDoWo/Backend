"""Tests for the LangChain agent tools exposed by ``build_agent``.

The LLM and the agent factory are stubbed out, so these tests never reach
OpenRouter.
"""

from typing import Any

import pytest

from api import langchain_setup
from api.panel_analyze import Control, ControlBox, ControlType, PanelControlError

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

    monkeypatch.setattr("api.panel_analyze.analyze_panel", fake_analyze_panel)
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

    monkeypatch.setattr("api.panel_analyze.analyze_panel", fake_analyze_panel)
    tool = _agent_tools(monkeypatch, image_path=None)["control_panel_analysis"]

    result = tool.func()

    assert result["controls"] == []
    assert "error" in result
    assert called is False


def test_control_panel_tool_degrades_when_analysis_fails(monkeypatch):
    def boom(*args: Any, **kwargs: Any) -> list[Control]:
        raise PanelControlError("the model returned an unusable control list")

    monkeypatch.setattr("api.panel_analyze.analyze_panel", boom)
    tool = _agent_tools(monkeypatch)["control_panel_analysis"]

    result = tool.func()

    assert result == {
        "controls": [],
        "error": "the model returned an unusable control list",
    }


def test_control_panel_tool_has_a_useful_description(monkeypatch):
    tool = _agent_tools(monkeypatch)["control_panel_analysis"]

    assert "control panel" in tool.description.lower()
