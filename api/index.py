import json
import pathlib
import sys
import types
from typing import Any

import auth
import config
import firebase_store
import langchain_setup
import logging_config
import panel_analyze
import pydantic
import request_helpers
import response_formatting
import schemas
import slowapi
from fastapi import FastAPI, HTTPException, Request, params, responses, status
from fastapi.middleware import cors
from slowapi import errors, util
from starlette import datastructures

# Force Python/Vercel to recognize the 'api' directory BEFORE importing local modules
API_DIR = pathlib.Path(__file__).resolve().parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

settings = config.get_settings()
app = FastAPI(title="HoDoWo backend")
logger = logging_config.get_logger(__name__)

def get_current_user(request: Request) -> dict:
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials"
    )

def _client_ip(request: Request) -> str:
    """Return the caller IP, honouring the proxy header Vercel sets."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return util.get_remote_address(request)


# Per-IP limiter for the AI endpoints. The counter lives in process memory:
# on Vercel each request may hit a different instance, so this is basic
# protection only. A shared store (Upstash Redis) is needed for a hard limit.
limiter = slowapi.Limiter(key_func=_client_ip)
ai_rate_limit = limiter.limit(settings.ai_rate_limit)
app.state.limiter = limiter
app.add_exception_handler(errors.RateLimitExceeded, slowapi._rate_limit_exceeded_handler)


@app.middleware("http")
async def jwt_interceptor(request: Request, call_next):
    """Reject every request that lacks a valid JWT before it reaches a route."""
    # CORS preflight requests never carry credentials.
    if request.method == "OPTIONS" or request.url.path in auth.PUBLIC_PATHS:
        return await call_next(request)

    try:
        # Claims are available to routes as request.state.user.
        request.state.user = auth.authenticate_request(request.headers.get("authorization"))
    except auth.AuthError as exc:
        return responses.JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    return await call_next(request)


# CORS is added after the JWT middleware so it is the outermost layer and can
# attach its headers to every response, including authentication errors. Only
# the browser origins listed in ALLOWED_ORIGINS are accepted, never a wildcard.
app.add_middleware(
    cors.CORSMiddleware,
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


@app.post("/api/feedback")
async def feedback(request: Request) -> dict[str, str]:
    """Store or acknowledge user rating and feedback."""
    payload = await request.json()
    logger.info("Recorded feedback: %s", payload)
    return {"status": "ok"}


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
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel analysis without an image")
        raise HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()
    language = str(form.get("language") or "").strip() or "en"

    image_path = await request_helpers.save_uploaded_image(image, "panel.jpg")
    try:
        controls = panel_analyze.analyze_panel(image_path, brand=brand, model=model, language=language)
        logger.info("Panel analysis detected %s controls", len(controls))
        return {"controls": [control.model_dump(mode="json") for control in controls]}
    except (panel_analyze.PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel analysis failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)


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
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel location without an image")
        raise HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()

    image_path = await request_helpers.save_uploaded_image(image, "appliance.jpg")
    try:
        box = panel_analyze.locate_panel(image_path, brand=brand, model=model)
        logger.info("Panel location found a panel: %s", box is not None)
        return {"box": box.model_dump(mode="json") if box else None}
    except (panel_analyze.PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel location failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)



@app.post("/api/manuals", response_model=dict)
async def create_manual(
    payload: schemas.SaveManualRequest, request: Request
) -> dict[str, Any]:
    """Explicitly save a manual when the client wants to persist one."""
    user = get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    try:
        manual_id = firebase_store.save_manual(uid, payload.model_dump(mode="json"))
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Manual save failed: %s", exc)
        raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {"status": "success", "manual_id": manual_id, "is_bookmarked": payload.is_bookmarked}


@app.patch("/api/manuals/{manual_id}/bookmark", response_model=dict)
async def toggle_bookmark(
    manual_id: str, req: schemas.BookmarkRequest, request: Request
) -> dict[str, Any]:
    """Set the bookmark state for a manual owned by the authenticated user."""
    user = get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    try:
        is_bookmarked = firebase_store.update_bookmark(uid, manual_id, req.is_bookmarked)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="manual not found") from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError) as exc:
        logger.error("Bookmark update failed: %s", exc)
        raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {
        "status": "success",
        "manual_id": manual_id,
        "is_bookmarked": is_bookmarked,
    }


@app.post("/api/manuals/{manual_id}/rate", response_model=dict)
async def rate_manual(
    manual_id: str, req: schemas.RatingRequest, request: Request
) -> dict[str, Any]:
    """Toggle or change the authenticated user's like/dislike rating."""
    user = get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    if req.rating not in (-1, 1):
        raise HTTPException(status_code=422, detail="rating must be 1 or -1")
    try:
        current_rating = firebase_store.set_rating(uid, manual_id, req.rating)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="manual not found") from exc
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Manual rating failed: %s", exc)
        raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {
        "status": "success",
        "manual_id": manual_id,
        "current_rating": current_rating,
    }

class EnvironmentalTipResponse(pydantic.BaseModel):
    tip_title: str
    tip_content: str
    category: str
    potential_savings: str

@app.get("/api/appliances/environmental-tip", response_model=EnvironmentalTipResponse)
async def get_environmental_tip(user: dict = params.Depends(get_current_user)):
    """
    Generates an electricity-saving and environmental home appliance tip
    dynamically using OpenRouter LLM integration.
    """
    try:
        completion = config.get_openrouter_llm().complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert in home appliance energy efficiency and environmental sustainability. "
                        "Provide a unique, practical electricity-saving or environmental tip/curiosity about home appliances. Just string "
                    )
                },
                {
                    "role": "user",
                    "content": "Generate an energy-saving home appliance tip or curiosity."
                }
            ],
        )

        raw_content = completion.choices[0].message.content
        if not raw_content:
            raise HTTPException(status_code=500, detail="Empty response received from OpenRouter.")

        parsed_data = json.loads(raw_content)

        return EnvironmentalTipResponse(
            tip_title=parsed_data.get("tip_title", "Home Appliance Efficiency Tip"),
            tip_content=parsed_data.get("tip_content", "No content provided."),
            category=parsed_data.get("category", "Energy Conservation"),
            potential_savings=parsed_data.get("potential_savings", "Varies")
        )

    except json.JSONDecodeError as json_exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse LLM JSON response: {str(json_exc)}"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OpenRouter integration error: {str(exc)}"
        )