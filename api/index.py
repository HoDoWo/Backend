import pathlib
import sys
from typing import Any

from endpoints import bookmarks, environmental_tip, locate_buttons
import auth
import config
import firebase_store
import logging_config
import schemas
import slowapi
from endpoints import rating
from fastapi import FastAPI, HTTPException, Request, responses, status
from fastapi.middleware import cors
from slowapi import errors, util

# Force Python/Vercel to recognize the 'api' directory BEFORE importing local modules
API_DIR = pathlib.Path(__file__).resolve().parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

logger = logging_config.get_logger(__name__)

settings = config.get_settings()
app = FastAPI(title="HoDoWo backend")

app.include_router(rating.rate_router)
app.include_router(bookmarks.bookmark_router)
app.include_router(locate_buttons.locate_router)
app.include_router(environmental_tip.environmental_tip_router)




# Per-IP limiter for the AI endpoints. The counter lives in process memory:
# on Vercel each request may hit a different instance, so this is basic
# protection only. A shared store (Upstash Redis) is needed for a hard limit.

app.state.limiter = config.limiter
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





@app.post("/api/manuals", response_model=dict)
async def create_manual(
    payload: schemas.SaveManualRequest, request: Request
) -> dict[str, Any]:
    """Explicitly save a manual when the client wants to persist one."""
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="authenticated user id is missing")
    try:
        manual_id = firebase_store.save_manual(uid, payload.model_dump(mode="json"))
    except (firebase_store.FirebaseConfigurationError, RuntimeError, ValueError) as exc:
        logger.error("Manual save failed: %s", exc)
        raise HTTPException(status_code=503, detail="manual persistence is unavailable") from exc
    return {"status": "success", "manual_id": manual_id, "is_bookmarked": payload.is_bookmarked}
