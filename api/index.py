from pathlib import Path
import tempfile
import json
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

if __package__:
    from .auth import PUBLIC_PATHS, AuthError, authenticate_request
    from .langchain_setup import build_agent
    from .logging_config import get_logger
else:
    from auth import PUBLIC_PATHS, AuthError, authenticate_request
    from langchain_setup import build_agent
    from logging_config import get_logger

app = FastAPI()
logger = get_logger(__name__)

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _find_generated_image(messages: list[object]) -> str | None:
    """Extract the latest image-generation result from LangChain tool messages."""
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
@app.middleware("http")
async def jwt_interceptor(request: Request, call_next):
    """Reject every request that lacks a valid JWT before it reaches a route."""
    # CORS preflight requests never carry credentials.
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    try:
        # Claims are available to routes as request.state.user.
        request.state.user = authenticate_request(request.headers.get("authorization"))
    except AuthError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    return await call_next(request)


def _find_uploaded_image(form: Mapping[str, object]) -> UploadFile | None:
    """Find an uploaded image regardless of the multipart field name."""
    for value in form.values():
        if isinstance(value, UploadFile) and value.content_type:
            if value.content_type.startswith("image/"):
                return value
    return None


@app.post("/api/chat")
async def chat(request: Request) -> dict[str, str | None]:
    """Run every text and/or image request through the LangChain agent."""
    image: UploadFile | None = None
    image_path: str | None = None

    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        image = _find_uploaded_image(form)
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""

    if not isinstance(message, str):
        raise HTTPException(status_code=422, detail="message must be text")

    if image is None and not message.strip():
        logger.warning("Rejected chat request without text or image")
        raise HTTPException(
            status_code=422,
            detail="provide at least one of image or message",
        )

    if image is not None:
        if not image.content_type or not image.content_type.startswith("image/"):
            logger.warning("Rejected chat request with invalid image content type")
            raise HTTPException(status_code=415, detail="image must be an image upload")

        content = await image.read()
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("Rejected chat request because image exceeds size limit")
            raise HTTPException(status_code=413, detail="image is larger than 15 MB")

        suffix = Path(image.filename or "appliance.jpg").suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(content)
            image_path = temporary.name

    try:
        prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
        if image_path is not None:
            prompt += f"\nUploaded image path: {image_path}"

        logger.info("Invoking LangChain agent (has_image=%s)", image_path is not None)
        result = build_agent(image_path).invoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
        logger.info("LangChain agent completed successfully")
        messages = result["messages"]
        answer = messages[-1].content
        generated_image = ensure_image_overlay(messages, image_path, message)
        return {
            "reply": answer,
            "image": generated_image or _find_generated_image(messages),
        }
    except RuntimeError as exc:
        logger.error("LangChain agent failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if image_path is not None:
            Path(image_path).unlink(missing_ok=True)