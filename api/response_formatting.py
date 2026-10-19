"""Convert LangChain messages into the Android manual-card response."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

if __package__:
    from .image_generation import _image_data_url
    from .langchain_setup import _json_tool_results
else:
    from image_generation import _image_data_url
    from langchain_setup import _json_tool_results


def find_generated_image(messages: list[object]) -> str | None:
    for message in reversed(messages):
        content = getattr(message, "content", None)
        candidates = content if isinstance(content, list) else [content]
        for candidate in candidates:
            value = candidate
            if isinstance(candidate, str):
                try:
                    value = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
            if isinstance(value, dict) and value.get("image_url"):
                return str(value["image_url"])
    return None


def format_chat_response(
    messages: list[object],
    image_path: str | None,
    message: str,
    generated_image: str | None = None,
) -> dict[str, Any]:
    tool_results = _json_tool_results(messages)
    manual_info = next(
        (
            item for item in reversed(tool_results)
            if isinstance(item.get("steps"), list) and item.get("steps")
        ),
        None,
    )
    appliance_info = next(
        (
            item for item in reversed(tool_results)
            if item.get("appliance_name") or item.get("brand") or item.get("model")
        ),
        manual_info,
    )
    image_url = generated_image or find_generated_image(messages)
    reply = assistant_text(messages)
    steps = format_steps(manual_info.get("steps", [])) if manual_info else []
    if not steps:
        steps = extract_text_steps(reply)
    if not steps:
        return {"reply": reply, "image": image_url}

    if not image_url and image_path and Path(image_path).is_file():
        try:
            if Path(image_path).stat().st_size <= 400 * 1024:
                image_url = _image_data_url(image_path)
        except OSError:
            pass

    appliance_name = format_appliance_name(appliance_info)
    return {
        "reply": reply,
        "is_manual": True,
        "appliance_name": appliance_name,
        "issue_title": format_issue_title(message, appliance_name),
        "overview": str((manual_info or {}).get("summary") or guide_overview(reply)),
        "steps": steps,
        "warnings": [
            str(warning)
            for warning in (manual_info or {}).get("warnings", [])
            if isinstance(warning, str)
        ],
        "image": image_url,
    }


def assistant_text(messages: list[object]) -> str:
    for message in reversed(messages):
        role = getattr(message, "type", None) or getattr(message, "role", None)
        if role not in (None, "ai", "assistant"):
            continue
        content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("text")
            )
        if content:
            return str(content).strip()
    return "Here is the guidance for your appliance."


def format_steps(raw_steps: list[Any]) -> list[dict[str, Any]]:
    steps = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if isinstance(raw_step, dict):
            steps.append({
                "number": raw_step.get("number", index),
                "title": str(raw_step.get("title") or f"Step {index}"),
                "description": str(raw_step.get("description") or raw_step.get("text") or ""),
            })
        elif isinstance(raw_step, str) and raw_step.strip():
            title, separator, description = raw_step.partition(":")
            steps.append({
                "number": index,
                "title": title.strip() if separator and len(title.strip()) <= 40 else f"Step {index}",
                "description": description.strip() if separator else raw_step.strip(),
            })
    return steps


def extract_text_steps(text: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r"(?m)^\s*(\d+)[.)]\s+(.+?)\s*$", text))
    steps = []
    for index, match in enumerate(matches, start=1):
        content = re.sub(r"\*{1,3}", "", match.group(2)).strip()
        title, separator, description = content.partition(":")
        steps.append({
            "number": index,
            "title": title.strip() if separator and len(title.strip()) <= 40 else f"Step {index}",
            "description": description.strip() if separator else content,
        })
    return steps


def format_appliance_name(info: dict[str, Any] | None) -> str:
    if not info:
        return "Household Appliance"
    name = " ".join(
        str(info.get(key) or "").strip()
        for key in ("brand", "appliance_name")
        if str(info.get(key) or "").strip()
    ) or "Household Appliance"
    model = str(info.get("model") or "").strip()
    return f"{name} ({model})" if model else name


def format_issue_title(message: str, appliance_name: str) -> str:
    clean = re.sub(r"^\s*(question|request|appliance)\s*:\s*", "", message, flags=re.I)
    clean = clean.splitlines()[0].strip() if clean else ""
    return clean[:80] if clean else f"{appliance_name} Guide"


def guide_overview(reply: str) -> str:
    for line in reply.splitlines():
        clean = line.strip().strip("*").strip()
        if clean and not re.match(r"^\d+[.)]\s+", clean):
            return clean
    return reply[:200]
