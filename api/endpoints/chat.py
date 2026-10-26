import os
import pathlib
from typing import Any

import auth
import config
import fastapi
import firebase_store
import langchain_setup
import logging_config
import reinforcement
import request_helpers
import response_formatting
import time_budget
from firebase_admin import storage
from starlette import datastructures

logger = logging_config.get_logger(__name__)


chat_router = fastapi.APIRouter()

@chat_router.post("/api/chat")
@config.ai_rate_limit
async def chat(request: fastapi.Request) -> dict[str, Any]:
    """Run every text and/or image request through the LangChain agent."""
    image: datastructures.UploadFile | None = None
    image_path: str | None = None
    history_raw: Any = None
    appliance_raw: Any = None

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        history_raw = form.get("history")
        appliance_raw = form.get("appliance")
        image = request_helpers.find_uploaded_image(form)
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""
        history_raw = body.get("history")
        appliance_raw = body.get("appliance")

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

    # Vercel stops the function after 300 s: the slow steps share this budget.
    time_budget.start()
    try:
        prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
        if image_path is not None:
            prompt += f"\nUploaded image path: {image_path}"

        # Rated guides of the same appliance (#40).
        prompt = reinforcement.add_to_prompt(
            prompt, reinforcement.appliance_name_from(appliance_raw)
        )

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
        formatted = _persist_generated_manual(request, formatted, image_path=image_path)
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
    request: fastapi.Request,
    formatted: dict[str, Any],
    image_path: str | None = None,
) -> dict[str, Any]:
    """Save a completed guide (and its image) without making AI responses depend on Firestore."""
    if not formatted.get("is_manual"):
        return formatted
    try:
        user = auth.get_current_user(request)
    except fastapi.HTTPException:
        user = {}
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        logger.error("Cannot persist manual because JWT has no uid or sub claim")
        return formatted

    image_bytes = None
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()

    try:
        manual_id = firebase_store.save_manual(uid, formatted, image_bytes=image_bytes)
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Generated manual could not be saved to Firebase: %s", exc)
        return formatted

    if image_bytes:
        try:
            blob = storage.bucket().blob(f"manuals/{uid}/{manual_id}.jpg")
            formatted["image"] = blob.generate_signed_url()
        except Exception as exc:
            logger.error("Manual image URL could not be generated: %s", exc)

    return {**formatted, "manual_id": manual_id}
