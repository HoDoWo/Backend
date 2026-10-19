from pathlib import Path
import tempfile
import json
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.datastructures import UploadFile

if __package__:
    from .auth import PUBLIC_PATHS, AuthError, authenticate_request
    from .config import get_settings
    from .langchain_setup import build_agent, ensure_image_overlay
    from .logging_config import get_logger
    from .panel_analyze import PanelControlError, analyze_panel, locate_panel
    from .ratings import (
        RATINGS_TO_READ,
        ApplianceRef,
        RatingIn,
        appliance_id,
        build_rating_context,
        build_record,
        get_store,
    )
else:
    from auth import PUBLIC_PATHS, AuthError, authenticate_request
    from config import get_settings
    from langchain_setup import build_agent, ensure_image_overlay
    from logging_config import get_logger
    from panel_analyze import PanelControlError, analyze_panel, locate_panel
    from ratings import (
        RATINGS_TO_READ,
        ApplianceRef,
        RatingIn,
        appliance_id,
        build_rating_context,
        build_record,
        get_store,
    )

settings = get_settings()
app = FastAPI(title="HoDoWo backend")
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
def _client_ip(request: Request) -> str:
    """Return the caller IP, honouring the proxy header Vercel sets."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return get_remote_address(request)


# Per-IP limiter for the AI endpoints. The counter lives in process memory:
# on Vercel each request may hit a different instance, so this is basic
# protection only. A shared store (Upstash Redis) is needed for a hard limit.
limiter = Limiter(key_func=_client_ip)
ai_rate_limit = limiter.limit(settings.ai_rate_limit)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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


# CORS is added after the JWT middleware so it is the outermost layer and can
# attach its headers to every response, including authentication errors. Only
# the browser origins listed in ALLOWED_ORIGINS are accepted, never a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Security headers found missing by the OWASP ZAP scan of Sprint 1 (#44).
# Registered last, so it is the outermost layer and also covers CORS,
# authentication and rate-limit error responses.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers and keep API responses out of shared caches."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    # Answers depend on the user's token: no browser or proxy may store them.
    response.headers.setdefault("Cache-Control", "no-store")
    return response

@app.get("/health")
async def health() -> dict[str, str]:
    """Report that the backend is up. Public: no token required."""
    return {"status": "ok"}


def _find_uploaded_image(form: Mapping[str, object]) -> UploadFile | None:
    """Find an uploaded image regardless of the multipart field name."""
    for value in form.values():
        if isinstance(value, UploadFile) and value.content_type:
            if value.content_type.startswith("image/"):
                return value
    return None


async def _save_uploaded_image(image: UploadFile, default_name: str) -> str:
    """Validate an uploaded image and store it in a temporary file."""
    if not image.content_type or not image.content_type.startswith("image/"):
        logger.warning("Rejected request with an invalid image content type")
        raise HTTPException(status_code=415, detail="image must be an image upload")

    content = await image.read()
    if len(content) > MAX_IMAGE_BYTES:
        logger.warning("Rejected request because the image exceeds the size limit")
        raise HTTPException(status_code=413, detail="image is larger than 15 MB")

    suffix = Path(image.filename or default_name).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(content)
        return temporary.name


def _parse_appliance(raw: object) -> ApplianceRef | None:
    """Read the optional "appliance" sent by the app with a question.

    JSON requests send it as an object; multipart requests (with a photo)
    send the same object as a JSON string in the "appliance" form field.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignored an appliance field that is not valid JSON")
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return ApplianceRef.model_validate(raw)
    except ValidationError:
        logger.warning("Ignored an invalid appliance object in a chat request")
        return None


def _rating_context_for(appliance: ApplianceRef | None) -> str:
    """Previous rated answers for the appliance, ready for the prompt (#40).

    Never blocks the chat: without Firestore, or on any storage error, the
    question is simply sent without this extra context.
    """
    key = appliance_id(appliance)
    if not key:
        return ""

    try:
        store = get_store()
        if store is None:
            return ""
        return build_rating_context(store.recent_for(key, RATINGS_TO_READ))
    except Exception as exc:  # noqa: BLE001 - the ratings are optional context
        logger.warning("Could not load previous ratings: %s", exc)
        return ""


@app.post("/api/chat")
@ai_rate_limit
async def chat(request: Request) -> dict[str, str | None]:
    """Run every text and/or image request through the LangChain agent."""
    image: UploadFile | None = None
    image_path: str | None = None
    appliance: ApplianceRef | None = None

    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        image = _find_uploaded_image(form)
        appliance = _parse_appliance(form.get("appliance"))
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""
        appliance = _parse_appliance(body.get("appliance"))

    if not isinstance(message, str):
        raise HTTPException(status_code=422, detail="message must be text")

    if image is None and not message.strip():
        logger.warning("Rejected chat request without text or image")
        raise HTTPException(
            status_code=422,
            detail="provide at least one of image or message",
        )

    if image is not None:
        image_path = await _save_uploaded_image(image, "appliance.jpg")

    try:
        prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
        if image_path is not None:
            prompt += f"\nUploaded image path: {image_path}"

        rating_context = _rating_context_for(appliance)
        if rating_context:
            prompt = f"{rating_context}\n\nUser question: {prompt}"

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


@app.post("/api/panel/analyze")
@ai_rate_limit
async def panel_analyze(request: Request) -> dict[str, list[dict[str, object]]]:
    """Detect the buttons, dials and other controls in a cropped panel photo."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(
            status_code=415,
            detail="send the panel image as multipart/form-data",
        )

    form = await request.form()
    image = _find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel analysis without an image")
        raise HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()
    language = str(form.get("language") or "").strip() or "en"

    image_path = await _save_uploaded_image(image, "panel.jpg")
    try:
        controls = analyze_panel(image_path, brand=brand, model=model, language=language)
        logger.info("Panel analysis detected %s controls", len(controls))
        return {"controls": [control.model_dump(mode="json") for control in controls]}
    except (PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel analysis failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        Path(image_path).unlink(missing_ok=True)


@app.post("/api/panel/locate")
@ai_rate_limit
async def panel_locate(request: Request) -> dict[str, dict[str, float] | None]:
    """Locate the control panel in a full appliance photo (S1-16).

    Returns its normalised box, or ``null`` when there is no panel, so the
    client can crop before calling ``/api/panel/analyze``.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(
            status_code=415,
            detail="send the photo as multipart/form-data",
        )

    form = await request.form()
    image = _find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel location without an image")
        raise HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()

    image_path = await _save_uploaded_image(image, "appliance.jpg")
    try:
        box = locate_panel(image_path, brand=brand, model=model)
        logger.info("Panel location found a panel: %s", box is not None)
        return {"box": box.model_dump(mode="json") if box else None}
    except (PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel location failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        Path(image_path).unlink(missing_ok=True)


@app.post("/api/feedback", status_code=201)
@ai_rate_limit
async def feedback(request: Request) -> dict[str, str]:
    """Store the user's rating of an AI answer (#40).

    Body: {"question", "answer", "rating": 1 | -1, "comment"?, "appliance"?}.
    The user comes from the verified token, never from the body.
    """
    try:
        rating = RatingIn.model_validate(await request.json())
    except ValueError as exc:  # invalid JSON or invalid fields
        raise HTTPException(status_code=422, detail="invalid feedback") from exc

    store = get_store()
    if store is None:
        raise HTTPException(status_code=503, detail="ratings storage is not configured")

    user_id = str(request.state.user.get("sub", ""))
    try:
        store.add(build_record(rating, user_id))
    except Exception as exc:  # noqa: BLE001 - report any storage failure as 502
        logger.error("Could not store the rating: %s", exc)
        raise HTTPException(status_code=502, detail="could not store the rating") from exc

    logger.info("Stored an AI answer rating (rating=%s)", rating.rating)
    return {"status": "stored"}
