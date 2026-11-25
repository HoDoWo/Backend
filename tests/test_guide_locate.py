"""Tests for locating guide-step controls in a live camera frame (AR auto-alignment)."""

import json

import pytest
from fastapi.testclient import TestClient

import guide_locate as guide_module
from guide_locate import GuideLocateError, GuideStep, StepPoint
from index import app
from jwt_helpers import bearer, make_token

client = TestClient(app)

STEPS = [
    {"step": 1, "title": "Open the door", "pin_x": 0.2, "pin_y": 0.3},
    {"step": 2, "title": "Wait", "description": "Leave it for 2 minutes"},
    {"step": 3, "title": "Press Start", "pin_x": 0.8, "pin_y": 0.9},
]


def _auth_headers():
    return bearer(make_token())


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """Returns the queued responses in order and records the conversation."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


@pytest.fixture
def frame(tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"frame-bytes")
    return str(path)


# --- request parsing --------------------------------------------------------


def test_parse_steps_reads_pins_and_clips_long_text():
    steps = guide_module.parse_steps(json.dumps(STEPS + [{"step": 4, "title": "x" * 1000}]))
    assert [step.step for step in steps] == [1, 2, 3, 4]
    assert steps[0].pin_x == 0.2
    assert steps[1].pin_x is None
    assert len(steps[3].title) == guide_module.MAX_TEXT_CHARS


@pytest.mark.parametrize("raw", ["", "not json", "{}", "[]", "[1, 2]", json.dumps([{"title": "no number"}])])
def test_parse_steps_rejects_unusable_input(raw):
    with pytest.raises(ValueError):
        guide_module.parse_steps(raw)


def test_parse_steps_caps_the_number_of_steps():
    too_many = [{"step": n} for n in range(guide_module.MAX_STEPS + 1)]
    with pytest.raises(ValueError):
        guide_module.parse_steps(json.dumps(too_many))


def test_parse_targets_defaults_to_all_steps_and_keeps_known_ones():
    steps = [GuideStep(step=n) for n in range(1, 7)]
    assert guide_module.parse_targets("", steps) == [1, 2, 3, 4]
    assert guide_module.parse_targets("3, 1, 3, 9", steps) == [3, 1]


@pytest.mark.parametrize("raw", ["9", "a,b"])
def test_parse_targets_rejects_unknown_or_invalid_steps(raw):
    with pytest.raises(ValueError):
        guide_module.parse_targets(raw, [GuideStep(step=1)])


# --- model answer -------------------------------------------------------------


def test_coerce_points_keeps_confident_in_frame_points_of_requested_steps():
    raw = {
        "points": [
            {"step": 3, "x": 0.71, "y": 0.42, "confidence": 0.9},
            {"step": "1", "x": 0.2, "y": 0.25},  # string number, no confidence: kept as sure
            {"step": 1, "x": 0.9, "y": 0.9, "confidence": 0.99},  # second answer for step 1: ignored
            {"step": 2, "x": 0.5, "y": 0.5, "confidence": 0.9},  # not requested
            {"step": 4, "x": 1.4, "y": 0.5, "confidence": 0.9},  # outside the frame
            {"step": 5, "x": 0.4, "y": 0.5, "confidence": 0.2},  # unsure
            {"step": True, "x": 0.4, "y": 0.5},  # not a number
            "garbage",
        ]
    }
    points = guide_module.coerce_points(raw, targets=[1, 3, 4, 5])
    assert points == [
        StepPoint(step=1, x=0.2, y=0.25, confidence=1.0),
        StepPoint(step=3, x=0.71, y=0.42, confidence=0.9),
    ]


def test_coerce_points_accepts_a_bare_list_and_ignores_other_shapes():
    assert guide_module.coerce_points([{"step": 1, "x": 0.5, "y": 0.5}], [1])[0].step == 1
    assert guide_module.coerce_points({"nothing": []}, [1]) == []
    assert guide_module.coerce_points("text", [1]) == []


def test_locate_sends_frame_reference_steps_and_pins(frame, tmp_path, monkeypatch):
    reference = tmp_path / "reference.jpg"
    reference.write_bytes(b"reference-bytes")
    model = FakeModel(FakeResponse('{"points": [{"step": 3, "x": 0.6, "y": 0.4, "confidence": 0.8}]}'))
    monkeypatch.setattr(guide_module, "_get_openrouter_llm", lambda: model)

    steps = guide_module.parse_steps(json.dumps(STEPS))
    points = guide_module.locate_steps(frame, steps, [1, 3], str(reference), "Bosch washing machine")

    assert points == [StepPoint(step=3, x=0.6, y=0.4, confidence=0.8)]
    human = model.calls[0][1][1]
    text = human[0]["text"]
    assert "Bosch washing machine" in text
    assert "Step 3: Press Start (on the reference photo at x=0.800, y=0.900)" in text
    assert "Step 2: Wait - Leave it for 2 minutes" in text
    assert "steps: 1, 3" in text
    images = [part for part in human if part["type"] == "image_url"]
    assert len(images) == 2  # camera frame first, then the reference photo


def test_locate_without_reference_sends_only_the_frame(frame, monkeypatch):
    model = FakeModel(FakeResponse('{"points": []}'))
    monkeypatch.setattr(guide_module, "_get_openrouter_llm", lambda: model)

    assert guide_module.locate_steps(frame, [GuideStep(step=1, title="Open")], [1]) == []
    assert len([part for part in model.calls[0][1][1] if part["type"] == "image_url"]) == 1


def test_locate_retries_once_after_invalid_json(frame, monkeypatch):
    model = FakeModel(FakeResponse("Sure! Here it is."), FakeResponse('```json\n{"points": [{"step": 1, "x": 0.1, "y": 0.2}]}\n```'))
    monkeypatch.setattr(guide_module, "_get_openrouter_llm", lambda: model)

    points = guide_module.locate_steps(frame, [GuideStep(step=1)], [1])
    assert [point.step for point in points] == [1]
    assert len(model.calls) == 2


def test_locate_raises_after_two_invalid_answers(frame, monkeypatch):
    model = FakeModel(FakeResponse("no"), FakeResponse("still no"))
    monkeypatch.setattr(guide_module, "_get_openrouter_llm", lambda: model)

    with pytest.raises(GuideLocateError):
        guide_module.locate_steps(frame, [GuideStep(step=1)], [1])


# --- endpoint -------------------------------------------------------------------


def test_endpoint_returns_the_points(monkeypatch):
    seen = {}

    def fake_locate(frame_path, steps, targets, reference_path=None, appliance=""):
        seen.update(targets=targets, reference=reference_path is not None, appliance=appliance)
        return [StepPoint(step=1, x=0.3, y=0.4, confidence=0.9)]

    monkeypatch.setattr("guide_locate.locate_steps", fake_locate)

    response = client.post(
        "/api/guide/locate",
        files={
            "image": ("frame.jpg", b"frame-bytes", "image/jpeg"),
            "reference": ("photo.jpg", b"photo-bytes", "image/jpeg"),
        },
        data={"steps": json.dumps(STEPS), "targets": "1,3", "appliance": "Oven"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"points": [{"step": 1, "x": 0.3, "y": 0.4, "confidence": 0.9}]}
    assert seen == {"targets": [1, 3], "reference": True, "appliance": "Oven"}


def test_endpoint_requires_the_frame_and_valid_steps():
    no_frame = client.post(
        "/api/guide/locate",
        files={"reference": ("photo.jpg", b"photo-bytes", "image/jpeg")},
        data={"steps": json.dumps(STEPS)},
        headers=_auth_headers(),
    )
    assert no_frame.status_code == 422

    bad_steps = client.post(
        "/api/guide/locate",
        files={"image": ("frame.jpg", b"frame-bytes", "image/jpeg")},
        data={"steps": "nope"},
        headers=_auth_headers(),
    )
    assert bad_steps.status_code == 422


def test_endpoint_rejects_json_bodies():
    response = client.post("/api/guide/locate", json={"steps": STEPS}, headers=_auth_headers())
    assert response.status_code == 415


def test_endpoint_reports_a_model_failure_as_bad_gateway(monkeypatch):
    def boom(*args, **kwargs):
        raise GuideLocateError("guide locate returned invalid JSON")

    monkeypatch.setattr("guide_locate.locate_steps", boom)
    response = client.post(
        "/api/guide/locate",
        files={"image": ("frame.jpg", b"frame-bytes", "image/jpeg")},
        data={"steps": json.dumps(STEPS)},
        headers=_auth_headers(),
    )
    assert response.status_code == 502


def test_endpoint_requires_auth():
    response = client.post(
        "/api/guide/locate",
        files={"image": ("frame.jpg", b"frame-bytes", "image/jpeg")},
        data={"steps": json.dumps(STEPS)},
    )
    assert response.status_code == 401
