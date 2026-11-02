"""Marketplace checkout, Stripe webhook and order history endpoints (#43)."""

from typing import Any

import auth
import checkout_service
import config
import fastapi
import firebase_store
import logging_config

logger = logging_config.get_logger(__name__)

checkout_router = fastapi.APIRouter()

# Public route (see auth.PUBLIC_PATHS): Stripe sends no JWT, the request is
# authenticated by its Stripe-Signature header instead.
WEBHOOK_PATH = "/api/marketplace/stripe/webhook"

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _uid(request: fastapi.Request) -> str:
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise fastapi.HTTPException(status_code=401, detail="authenticated user id is missing")
    return uid


def _run(action, *args):
    try:
        return action(*args)
    except checkout_service.CheckoutError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("Marketplace checkout is misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="payments are unavailable") from exc


@checkout_router.post("/api/marketplace/checkout", response_model=dict)
async def create_checkout(request: fastapi.Request) -> dict[str, Any]:
    """Create the orders for the user's cart and the PaymentIntent to pay them."""
    return _run(checkout_service.checkout, _uid(request))


@checkout_router.post(WEBHOOK_PATH, response_model=dict)
async def stripe_webhook(request: fastapi.Request) -> dict[str, Any]:
    """Receive Stripe events; the raw body is needed to verify the signature."""
    payload = await request.body()
    return _run(checkout_service.handle_webhook, payload, request.headers.get("stripe-signature"))


@checkout_router.get("/api/marketplace/orders", response_model=dict)
async def list_orders(request: fastapi.Request) -> dict[str, Any]:
    """Return the user's purchase history, newest first."""
    return {"orders": _run(checkout_service.list_orders, _uid(request))}


@checkout_router.get("/api/marketplace/orders/{order_id}", response_model=dict)
async def get_order(order_id: str, request: fastapi.Request) -> dict[str, Any]:
    """Return one of the user's orders."""
    return _run(checkout_service.get_order, _uid(request), order_id)
