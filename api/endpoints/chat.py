import pathlib
from typing import Any

import fastapi
import langchain_setup
import request_helpers
import response_formatting
from index import (
    ai_rate_limit,
    app,
    get_current_user,
    logger,
)
from starlette import datastructures

from api import firebase_store

@app.post("/api/chat")
@ai_rate_limit
async def chat(request: fastapi.Request) -> dict[str, Any]:
    """Run every text and/or image request through the LangChain agent."""
    image: datastructures.UploadFile | None = None
    image_path: str | None = None
    history_raw: Any = None

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        history_raw = form.get("history")
        image = request_helpers.find_uploaded_image(form)
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""
        history_raw = body.get("history")

    if not isinstance(message, str):
        raise fastapi.HTTPException(status_code=422, detail="message must be text")

    if image is None and not message.strip():
        logger.warning("Rejected chat request without text or image")
        raise fastapi.HTTPException(
            status_code=422,
            detail="provide at least one of image or message",
        )

    if image is not None:
        image_path = await request_helpers.save_uploaded_image(image, "appliance.jpg")

    try:
        prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
        if image_path is not None:
            prompt += f"\nUploaded image path: {image_path}"

        conversation_messages = request_helpers.parse_history(history_raw)
        conversation_messages.append({"role": "user", "content": prompt})

        logger.info("Invoking LangChain agent (has_image=%s, history_len=%d)", image_path is not None, len(conversation_messages) - 1)
        result = langchain_setup.build_agent(image_path).invoke(
            {"messages": conversation_messages}
        )
        logger.info("LangChain agent completed successfully")
        messages = result["messages"]
        generated_image = langchain_setup.ensure_image_overlay(messages, image_path, message)
        formatted = response_formatting.format_chat_response(messages, image_path, message, generated_image)
        formatted = _persist_generated_manual(request, formatted)
        if request.query_params.get("structured") == "true" or request.headers.get("x-response-format") == "structured":
            return formatted
        return formatted
    except RuntimeError as exc:
        logger.error("LangChain agent failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if image_path is not None:
            pathlib.Path(image_path).unlink(missing_ok=True)

def _persist_generated_manual(
    request: fastapi.Request, formatted: dict[str, Any]
) -> dict[str, Any]:
    """Save a completed guide without making AI responses depend on Firestore."""
    if not formatted.get("is_manual"):
        return formatted
    try:
        user = get_current_user(request)
    except fastapi.HTTPException:
        user = {}
    uid = str(user.get("uid") or user.get("sub") or request.headers.get("x-user-id") or "").strip()
    if not uid:
        logger.error("Cannot persist manual because JWT has no uid or sub claim")
        return formatted
    try:
        manual_id = firebase_store.save_manual(uid, formatted)
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Generated manual could not be saved to Firebase: %s", exc)
        return formatted
    return {**formatted, "manual_id": manual_id}