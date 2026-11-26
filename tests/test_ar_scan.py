"""Tests for the AR scan: device name and main controls in a camera frame."""

import pytest
from fastapi.testclient import TestClient

import ar_scan as scan_module
from ar_scan import ArScan, ArScanError, ScannedControl, ScannedDevice
from index import app
from jwt_helpers import bearer, make_token

client = TestClient(app)


def _auth_headers():
    return bearer(make_token())


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeModel:
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


def test_scan_keeps_named_device_and_valid_controls():
    scan = scan_module.coerce_scan(
        {
            "device": {"name": "Washing machine", "brand": "Bosch", "model": "null"},
            "controls": [
                {"label": "Start/Pause", "description": "Starts the cycle", "x": 0.7, "y": 0.3},
                {"label": "start/pause", "x": 0.1, "y": 0.1},  # same label again
                {"label": "Programme dial", "x": "0.4", "y": 0.35},  # number as text
                {"label": "", "x": 0.5, "y": 0.5},  # no label
                {"label": "Door", "x": 1.3, "y": 0.5},  # outside the frame
                {"label": "Temp", "x": None, "y": 0.5},
                "garbage",
            ],
        }
    )
    assert scan.device == ScannedDevice(name="Washing machine", brand="Bosch", model=None)
    assert [control.label for control in scan.controls] == ["Start/Pause", "Programme dial"]
    assert scan.controls[1].x == 0.4


def test_scan_without_a_device_has_no_controls():
    scan = scan_module.coerce_scan({"device": None, "controls": [{"label": "Button", "x": 0.5, "y": 0.5}]})
    assert scan == ArScan(device=None, controls=[])
    assert scan_module.coerce_scan({"device": {"name": " "}}).device is None
    assert scan_module.coerce_scan({}).controls == []


def test_scan_caps_controls_and_text_lengths():
    controls = [{"label": f"Key {n} " + "x" * 50, "description": "d" * 500, "x": 0.5, "y": 0.5} for n in range(20)]
    scan = scan_module.coerce_scan({"device": {"name": "Keyboard"}, "controls": controls})
    assert len(scan.controls) == scan_module.MAX_CONTROLS
    assert all(len(control.label) <= scan_module.MAX_LABEL_CHARS for control in scan.controls)
    assert all(len(control.description) <= scan_module.MAX_DESCRIPTION_CHARS for control in scan.controls)


def test_scan_frame_asks_in_the_requested_language(frame, monkeypatch):
    model = FakeModel(
        FakeResponse('{"device": {"name": "Teclado"}, "controls": [{"label": "Intro", "x": 0.8, "y": 0.6}]}')
    )
    monkeypatch.setattr(scan_module, "_get_openrouter_llm", lambda: model)

    scan = scan_module.scan_frame(frame, language="es")

    assert scan.device.name == "Teclado"
    assert scan.controls == [ScannedControl(label="Intro", description="", x=0.8, y=0.6)]
    system, human = model.calls[0]
    assert "Spanish" in system[1]
    assert human[1][1]["type"] == "image_url"


def test_scan_frame_retries_once_then_fails(frame, monkeypatch):
    model = FakeModel(FakeResponse("nope"), FakeResponse('```json\n{"device": null, "controls": []}\n```'))
    monkeypatch.setattr(scan_module, "_get_openrouter_llm", lambda: model)
    assert scan_module.scan_frame(frame).device is None
    assert len(model.calls) == 2

    broken = FakeModel(FakeResponse("no"), FakeResponse("still no"))
    monkeypatch.setattr(scan_module, "_get_openrouter_llm", lambda: broken)
    with pytest.raises(ArScanError):
        scan_module.scan_frame(frame)


def test_endpoint_returns_the_scan(monkeypatch):
    seen = {}

    def fake_scan(frame_path, language="en"):
        seen["language"] = language
        return ArScan(
            device=ScannedDevice(name="Oven", brand="Balay"),
            controls=[ScannedControl(label="Light", description="Turns the light on", x=0.2, y=0.3)],
        )

    monkeypatch.setattr("ar_scan.scan_frame", fake_scan)
    response = client.post(
        "/api/ar/scan",
        files={"image": ("frame.jpg", b"frame-bytes", "image/jpeg")},
        data={"language": "it"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    assert response.json() == {
        "device": {"name": "Oven", "brand": "Balay", "model": None},
        "controls": [{"label": "Light", "description": "Turns the light on", "x": 0.2, "y": 0.3}],
    }
    assert seen == {"language": "it"}


def test_endpoint_errors():
    assert client.post("/api/ar/scan", json={}, headers=_auth_headers()).status_code == 415
    no_image = client.post(
        "/api/ar/scan", files={"other": ("a.txt", b"x", "text/plain")}, headers=_auth_headers()
    )
    assert no_image.status_code == 422
    assert client.post("/api/ar/scan", files={"image": ("f.jpg", b"x", "image/jpeg")}).status_code == 401


def test_endpoint_reports_a_model_failure_as_bad_gateway(monkeypatch):
    def boom(*args, **kwargs):
        raise ArScanError("AR scan returned invalid JSON")

    monkeypatch.setattr("ar_scan.scan_frame", boom)
    response = client.post(
        "/api/ar/scan", files={"image": ("f.jpg", b"x", "image/jpeg")}, headers=_auth_headers()
    )
    assert response.status_code == 502


def test_controls_use_the_centre_of_their_0_1000_box():
    scan = scan_module.coerce_scan(
        {
            "device": {"name": "Washing machine"},
            "controls": [
                {"label": "Start", "box": [400, 700, 500, 800]},  # centre (0.75, 0.45)
                {"label": "Dial", "box_2d": ["100", "200", "300", "400"]},  # Gemini's key, numbers as text
                {"label": "Bad box", "box": [500, 700, 400, 800], "x": 0.1, "y": 0.2},  # ymin > ymax: use x/y
                {"label": "Out", "box": [0, 0, 2000, 10]},  # outside the grid, no x/y: dropped
            ],
        }
    )
    assert [(c.label, c.x, c.y) for c in scan.controls] == [
        ("Start", 0.75, 0.45),
        ("Dial", 0.3, 0.2),
        ("Bad box", 0.1, 0.2),
    ]


def test_prompt_asks_for_boxes_and_a_single_language():
    prompt = scan_module.SYSTEM_PROMPT.format(max_controls=8, language="Italian")
    assert "[ymin, xmin, ymax, xmax]" in prompt
    assert "never mix languages" in prompt
    assert "Italian" in prompt
