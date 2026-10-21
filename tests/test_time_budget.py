"""The request time budget that keeps photo questions under Vercel's 300 s."""

import contextvars
import json
import math
import types

import pytest

import image_generation as image_module
import langchain_setup
import time_budget


@pytest.fixture(autouse=True)
def fresh_context():
    # Each test runs in its own context, so a deadline never leaks to other tests.
    context = contextvars.copy_context()
    yield context


def run(context, function):
    return context.run(function)


def test_there_is_no_limit_outside_a_request(fresh_context):
    assert run(fresh_context, time_budget.remaining) == math.inf


def test_the_budget_counts_down(fresh_context):
    def check():
        time_budget.start(100)
        return time_budget.remaining()

    assert 99 < run(fresh_context, check) <= 100


def test_the_default_budget_leaves_a_margin_before_vercel_stops(fresh_context, monkeypatch):
    monkeypatch.delenv("REQUEST_BUDGET_SECONDS", raising=False)

    def check():
        time_budget.start()
        return time_budget.remaining()

    assert run(fresh_context, check) <= 300 - 50


def test_the_overlay_is_skipped_when_time_is_short(fresh_context, tmp_path, monkeypatch):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(image_module, "_upload_image", lambda path: pytest.fail("Kie must not be called"))

    def generate():
        time_budget.start(image_module.MIN_OVERLAY_SECONDS - 1)
        image_module.generate_overlay(str(image), "Set 180 degrees", ["Turn the knob"])

    with pytest.raises(RuntimeError, match="Not enough time"):
        run(fresh_context, generate)


def test_waiting_for_kie_stops_before_the_budget_ends(fresh_context, monkeypatch):
    monkeypatch.setenv("KIE_API_KEY", "test-key")
    monkeypatch.setenv("KIE_TIMEOUT_SECONDS", "600")
    polls = []

    class Pending:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "data": {"state": "generating"}}

    monkeypatch.setattr(image_module.requests, "get", lambda *a, **k: polls.append(1) or Pending())
    monkeypatch.setattr(image_module.time, "sleep", lambda seconds: None)

    def wait():
        # Only 1 s more than the finishing margin: the wait must stop almost at once.
        time_budget.start(image_module.FINISH_MARGIN_SECONDS + 0.05)
        image_module._wait_for_task("task-1")

    with pytest.raises(RuntimeError, match="Timed out"):
        run(fresh_context, wait)


def test_a_failed_overlay_is_not_tried_again_after_the_agent(monkeypatch, tmp_path):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        langchain_setup, "generate_overlay", lambda *args: pytest.fail("the overlay must not run twice")
    )
    failed_tool = types.SimpleNamespace(
        content=json.dumps({"image_url": None, "error": "The annotated image is not available: timeout"})
    )

    assert langchain_setup.ensure_image_overlay([failed_tool], str(image), "Set 180 degrees") is None
