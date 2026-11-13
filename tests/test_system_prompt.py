"""The agent answers follow-up questions from the chat instead of asking for the photo again."""

from langchain_setup import SYSTEM_PROMPT


def test_follow_ups_are_answered_from_the_history():
    assert "Never ask the user to send again a photo that is already in the history" in SYSTEM_PROMPT
    assert "give the typical value" in SYSTEM_PROMPT


def test_tool_names_are_not_shown_to_the_user():
    assert "never mention your tools or their names to the user" in SYSTEM_PROMPT
