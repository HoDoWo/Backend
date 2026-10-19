from api import langchain_setup as langchain_module


def test_ensure_image_overlay_runs_missing_workflow(monkeypatch, tmp_path):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image")
    calls = []

    monkeypatch.setattr(
        langchain_module,
        "retrieve_manual",
        lambda request, path: {
            "summary": "Use the visible controls.",
            "steps": ["Press the brew button."],
            "warnings": [],
            "needs_clarification": True,
            "confidence": 0.2,
        },
    )
    monkeypatch.setattr(
        langchain_module,
        "generate_overlay",
        lambda path, instructions, steps: (
            calls.append((path, instructions, steps))
            or {"image_url": "https://output.example/guide.png"}
        ),
    )

    result = langchain_module.ensure_image_overlay(
        [], str(image), "How do I prepare coffee?"
    )

    assert result == "https://output.example/guide.png"
    assert calls == [
        (str(image), "Use the visible controls.", ["Press the brew button."])
    ]


def test_ensure_image_overlay_converts_incomplete_manual_context(
    monkeypatch, tmp_path
):
    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image")
    calls = []

    def incomplete_manual(request, path):
        raise ValueError("identification is missing required appliance fields")

    monkeypatch.setattr(langchain_module, "retrieve_manual", incomplete_manual)
    monkeypatch.setattr(
        langchain_module,
        "generate_overlay",
        lambda path, instructions, steps: (
            calls.append((path, instructions, steps))
            or {"image_url": "https://output.example/clarification.png"}
        ),
    )

    result = langchain_module.ensure_image_overlay(
        [], str(image), "How do I prepare coffee?"
    )

    assert result == "https://output.example/clarification.png"
    assert calls[0][0] == str(image)
    assert "missing required appliance fields" in calls[0][1]
