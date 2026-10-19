"""Shared request parsing and temporary upload helpers."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from starlette.datastructures import UploadFile

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def find_uploaded_image(form: Mapping[str, object]) -> UploadFile | None:
    for value in form.values():
        if isinstance(value, UploadFile) and value.content_type and value.content_type.startswith("image/"):
            return value
    return None


async def save_uploaded_image(image: UploadFile, default_name: str) -> str:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="image must be an image upload")
    content = await image.read()
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image is larger than 15 MB")
    suffix = Path(image.filename or default_name).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(content)
        return temporary.name


def parse_history(raw_history: Any) -> list[dict[str, str]]:
    if isinstance(raw_history, str):
        try:
            raw_history = json.loads(raw_history)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw_history, list):
        return []
    parsed = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("sender") or "").strip().lower()
        content = str(item.get("content") or item.get("text") or "").strip()
        if content and role in ("user", "human", "assistant", "ai", "bot", "system"):
            parsed.append({
                "role": "user" if role in ("user", "human") else "assistant",
                "content": content,
            })
    return parsed
