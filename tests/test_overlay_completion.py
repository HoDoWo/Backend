import langchain_setup as langchain_module


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


def test_the_manual_is_kept_when_the_overlay_fails(monkeypatch, tmp_path):
    """No Kie credits: the answer still has the steps and pins of the manual."""
    from response_formatting import format_chat_response

    image = tmp_path / "appliance.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        langchain_module,
        "retrieve_manual",
        lambda request, path: {
            "appliance_name": "Washing Machine",
            "brand": "VAPORE",
            "summary": "Select 'Rapido 30' and press start.",
            "steps": ["Turn the main dial to 'Rapido 30'.", "Press the Start/Pause button."],
            "pins": [{"step": 1, "x": 0.2, "y": 0.5}],
            "warnings": [],
            "needs_clarification": False,
            "confidence": 0.9,
        },
    )

    def no_credits(path, instructions, steps):
        raise RuntimeError("Kie task creation failed: Credits insufficient")

    monkeypatch.setattr(langchain_module, "generate_overlay", no_credits)
    messages = []  # the agent answered nothing and called no tool

    image_url = langchain_module.ensure_image_overlay(messages, str(image), "How can I do a rapid wash?")
    formatted = format_chat_response(messages, None, "How can I do a rapid wash?", image_url)

    assert image_url is None
    assert formatted["is_manual"] is True
    assert formatted["overview"] == "Select 'Rapido 30' and press start."
    assert [step["description"] for step in formatted["steps"]] == [
        "Turn the main dial to 'Rapido 30'.", "Press the Start/Pause button.",
    ]
    assert formatted["steps"][0]["pin"] == {"x": 0.2, "y": 0.5}
    assert formatted["appliance_name"] == "VAPORE Washing Machine"
    # The agent wrote nothing: the reply is the manual's summary, not its raw JSON.
    assert formatted["reply"] == "Select 'Rapido 30' and press start."
