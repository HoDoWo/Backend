"""Tests for control panel location and button/dial detection (S1-16, S1-14)."""

import json

import pytest
from fastapi.testclient import TestClient

import panel_analyze as panel_module
from index import app
from panel_analyze import (
    MAX_CONTROLS,
    Control,
    ControlBox,
    ControlType,
    PanelControlError,
)
from jwt_helpers import bearer, make_token

client = TestClient(app)


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


def _one_control():
    return {
        "controls": [
            {
                "id": "c1",
                "label": "Start/Pause",
                "type": "button",
                "description": "Starts or pauses the selected program",
                "box": {"x": 0.62, "y": 0.40, "w": 0.10, "h": 0.12},
            }
        ]
    }


# --- parsing and clamping -------------------------------------------------


def test_coerce_controls_clamps_coordinates_to_the_image():
    controls = panel_module.coerce_controls(
        [
            {
                "id": "whatever",
                "label": "Temperature dial",
                "type": "dial",
                "description": "Sets the temperature",
                "box": {"x": -0.5, "y": 1.4, "w": 2.0, "h": 0.2},
            }
        ]
    )

    assert len(controls) == 1
    box = controls[0].box
    assert 0 <= box.x <= 1
    assert 0 <= box.y <= 1
    assert box.x + box.w <= 1.0
    assert box.y + box.h <= 1.0


def test_coerce_controls_renumbers_ids_and_drops_bad_items():
    controls = panel_module.coerce_controls(
        [
            {"label": "", "type": "button", "box": {"x": 0, "y": 0, "w": 0.1, "h": 0.1}},
            {"label": "No box", "type": "button"},
            {"label": "Zero size", "type": "button", "box": {"x": 0, "y": 0, "w": 0, "h": 0}},
            {
                "id": "zzz",
                "label": "Start",
                "type": "unsupported",
                "box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
            },
        ]
    )

    assert [control.id for control in controls] == ["c1"]
    assert controls[0].label == "Start"
    assert controls[0].type is ControlType.other


def test_coerce_controls_caps_the_list_size():
    raw = [
        {
            "label": f"Button {index}",
            "type": "button",
            "box": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},
        }
        for index in range(MAX_CONTROLS + 10)
    ]

    assert len(panel_module.coerce_controls(raw)) == MAX_CONTROLS


def test_coerce_controls_rejects_non_list_payloads():
    assert panel_module.coerce_controls("not a list") == []
    assert panel_module.coerce_controls(None) == []


def test_coerce_controls_handles_non_numeric_coordinates():
    controls = panel_module.coerce_controls(
        [
            {
                "label": "Dial",
                "type": "dial",
                "box": {"x": "abc", "y": None, "w": 0.2, "h": 0.2},
            }
        ]
    )

    assert controls[0].box.x == 0.0
    assert controls[0].box.y == 0.0


# --- model interaction ----------------------------------------------------


def test_analyze_panel_sends_image_context_and_language(tmp_path, monkeypatch):
    image = tmp_path / "panel.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(json.dumps(_one_control())))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    controls = panel_module.analyze_panel(
        str(image), brand="HoDoWo", model="HW-100", language="es"
    )

    assert controls[0].label == "Start/Pause"
    human_content = model.calls[0][1][1]
    assert human_content[1]["type"] == "image_url"
    assert "HoDoWo" in human_content[0]["text"]
    assert "HW-100" in human_content[0]["text"]
    assert "Spanish" in human_content[0]["text"]


def test_analyze_panel_retries_once_after_invalid_json(tmp_path, monkeypatch):
    image = tmp_path / "panel.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(
        FakeResponse("this is not json"),
        FakeResponse(json.dumps(_one_control())),
    )
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    controls = panel_module.analyze_panel(str(image))

    assert len(controls) == 1
    assert len(model.calls) == 2


def test_analyze_panel_raises_after_two_invalid_answers(tmp_path, monkeypatch):
    image = tmp_path / "panel.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse("nope"), FakeResponse("still nope"))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    with pytest.raises(PanelControlError):
        panel_module.analyze_panel(str(image))

    assert len(model.calls) == 2


def test_analyze_panel_accepts_code_fenced_json(tmp_path, monkeypatch):
    image = tmp_path / "panel.png"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(f"```json\n{json.dumps(_one_control())}\n```"))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    assert len(panel_module.analyze_panel(str(image))) == 1


def test_analyze_panel_without_image_returns_empty_list():
    assert panel_module.analyze_panel(None) == []


# --- endpoint -------------------------------------------------------------


def _auth_headers():
    return bearer(make_token())


def _fake_controls():
    return [
        Control(
            id="c1",
            label="Start/Pause",
            type=ControlType.button,
            description="Starts or pauses the selected program",
            box=ControlBox(x=0.62, y=0.40, w=0.10, h=0.12),
        ),
        Control(
            id="c2",
            label="Temperature dial",
            type=ControlType.dial,
            description="Selects the wash temperature",
            box=ControlBox(x=0.10, y=0.20, w=0.12, h=0.12),
        ),
    ]


def test_panel_endpoint_returns_controls(monkeypatch):
    monkeypatch.setattr("panel_analyze.analyze_panel", lambda *args, **kwargs: _fake_controls())

    response = client.post(
        "/api/panel/analyze",
        files={"image": ("panel.jpg", b"image-bytes", "image/jpeg")},
        data={"brand": "HoDoWo", "model": "HW-100", "language": "es"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert [control["id"] for control in payload["controls"]] == ["c1", "c2"]
    assert payload["controls"][0]["type"] == "button"
    for control in payload["controls"]:
        box = control["box"]
        assert 0 <= box["x"] <= 1 and 0 <= box["y"] <= 1
        assert 0 <= box["w"] <= 1 and 0 <= box["h"] <= 1


def test_panel_endpoint_requires_auth():
    response = client.post(
        "/api/panel/analyze",
        files={"image": ("panel.jpg", b"image-bytes", "image/jpeg")},
    )

    assert response.status_code == 401


def test_panel_endpoint_returns_empty_list_for_a_non_panel(monkeypatch):
    monkeypatch.setattr("panel_analyze.analyze_panel", lambda *args, **kwargs: [])

    response = client.post(
        "/api/panel/analyze",
        files={"image": ("panel.jpg", b"image-bytes", "image/jpeg")},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"controls": []}


def test_panel_endpoint_returns_502_when_the_model_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise PanelControlError("panel analysis returned invalid JSON")

    monkeypatch.setattr("panel_analyze.analyze_panel", boom)

    response = client.post(
        "/api/panel/analyze",
        files={"image": ("panel.jpg", b"image-bytes", "image/jpeg")},
        headers=_auth_headers(),
    )

    assert response.status_code == 502
    assert "invalid JSON" in response.json()["detail"]


def test_panel_endpoint_requires_an_image():
    response = client.post(
        "/api/panel/analyze",
        files={"brand": (None, "HoDoWo")},
        headers=_auth_headers(),
    )

    assert response.status_code == 422


def test_panel_endpoint_rejects_json_bodies():
    response = client.post(
        "/api/panel/analyze",
        json={"message": "hi"},
        headers=_auth_headers(),
    )

    assert response.status_code == 415


# --- panel location (S1-16) -----------------------------------------------


def _one_panel_box():
    return {"box": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}}


def test_coerce_panel_box_clamps_coordinates_to_the_image():
    box = panel_module.coerce_panel_box({"x": -0.2, "y": 0.9, "w": 2.0, "h": 0.4})

    assert box is not None
    assert 0 <= box.x <= 1
    assert 0 <= box.y <= 1
    assert box.x + box.w <= 1.0
    assert box.y + box.h <= 1.0


def test_coerce_panel_box_rejects_specks_and_degenerate_boxes():
    assert panel_module.coerce_panel_box({"x": 0.5, "y": 0.5, "w": 0.01, "h": 0.01}) is None
    assert panel_module.coerce_panel_box({"x": 0, "y": 0, "w": 0, "h": 0.5}) is None
    assert panel_module.coerce_panel_box(None) is None


def test_locate_panel_sends_image_context(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse(json.dumps(_one_panel_box())))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    box = panel_module.locate_panel(str(image), brand="HoDoWo", model="HW-100")

    assert box is not None
    assert box.x == pytest.approx(0.1)
    assert box.h == pytest.approx(0.5)
    human_content = model.calls[0][1][1]
    assert human_content[1]["type"] == "image_url"
    assert "HoDoWo" in human_content[0]["text"]
    assert "HW-100" in human_content[0]["text"]


def test_locate_panel_returns_none_when_there_is_no_panel(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse('{"box": null}'))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    assert panel_module.locate_panel(str(image)) is None


def test_locate_panel_accepts_a_bare_box(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse('{"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.4}'))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    box = panel_module.locate_panel(str(image))

    assert box is not None
    assert box.w == pytest.approx(0.5)


def test_locate_panel_retries_once_after_invalid_json(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(
        FakeResponse("this is not json"),
        FakeResponse(json.dumps(_one_panel_box())),
    )
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    box = panel_module.locate_panel(str(image))

    assert box is not None
    assert len(model.calls) == 2


def test_locate_panel_raises_after_two_invalid_answers(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"image-bytes")
    model = FakeModel(FakeResponse("nope"), FakeResponse("still nope"))
    monkeypatch.setattr(panel_module, "_get_openrouter_llm", lambda: model)

    with pytest.raises(PanelControlError):
        panel_module.locate_panel(str(image))

    assert len(model.calls) == 2


def test_locate_panel_without_image_returns_none():
    assert panel_module.locate_panel(None) is None


def test_panel_locate_endpoint_returns_the_box(monkeypatch):
    monkeypatch.setattr(
        "panel_analyze.locate_panel",
        lambda *args, **kwargs: ControlBox(x=0.1, y=0.2, w=0.6, h=0.5),
    )

    response = client.post(
        "/api/panel/locate",
        files={"image": ("photo.jpg", b"image-bytes", "image/jpeg")},
        data={"brand": "HoDoWo", "model": "HW-100"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"box": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}}


def test_panel_locate_endpoint_returns_null_without_a_panel(monkeypatch):
    monkeypatch.setattr("panel_analyze.locate_panel", lambda *args, **kwargs: None)

    response = client.post(
        "/api/panel/locate",
        files={"image": ("photo.jpg", b"image-bytes", "image/jpeg")},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"box": None}


def test_panel_locate_endpoint_requires_auth():
    response = client.post(
        "/api/panel/locate",
        files={"image": ("photo.jpg", b"image-bytes", "image/jpeg")},
    )

    assert response.status_code == 401


def test_panel_locate_endpoint_requires_an_image():
    response = client.post(
        "/api/panel/locate",
        files={"brand": (None, "HoDoWo")},
        headers=_auth_headers(),
    )

    assert response.status_code == 422


def test_panel_locate_endpoint_rejects_json_bodies():
    response = client.post(
        "/api/panel/locate",
        json={"x": 0.1},
        headers=_auth_headers(),
    )

    assert response.status_code == 415


def test_panel_locate_endpoint_returns_502_when_the_model_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise PanelControlError("panel location returned invalid JSON")

    monkeypatch.setattr("panel_analyze.locate_panel", boom)

    response = client.post(
        "/api/panel/locate",
        files={"image": ("photo.jpg", b"image-bytes", "image/jpeg")},
        headers=_auth_headers(),
    )

    assert response.status_code == 502
    assert "invalid JSON" in response.json()["detail"]
