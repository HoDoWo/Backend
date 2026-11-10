"""Thin wrapper around the Stripe SDK used by the marketplace checkout (#43).

Everything that talks to Stripe lives here, so the tests can replace this
module and never reach the network. Card data never passes through the
backend: the app collects it in the Stripe PaymentSheet and we only see the
PaymentIntent.
"""

from __future__ import annotations

from typing import Any

import stripe


class PaymentGatewayError(RuntimeError):
    """Stripe refused or could not process a request."""


class InvalidWebhookError(ValueError):
    """The webhook payload or its Stripe-Signature header is not valid."""


def create_payment_intent(
    *,
    secret_key: str,
    amount_cents: int,
    currency: str,
    metadata: dict[str, str],
    idempotency_key: str,
    allowed_payment_method_types: list[str] | None = None,
) -> dict[str, Any]:
    """Create a PaymentIntent and return its id and client secret.

    With ``allowed_payment_method_types`` the PaymentSheet offers only those (the
    method the buyer chose in the app) that are also enabled in the Stripe
    dashboard; without it, every method enabled there.

    Since API version 2026-09-30.endive (pinned by stripe-python 16) Stripe
    refuses the old ``payment_method_types`` parameter with a 400
    (``payment_method_types_no_longer_supported``), so it is never sent.
    """
    client = stripe.StripeClient(secret_key)
    params: dict[str, Any] = {"amount": amount_cents, "currency": currency, "metadata": metadata}
    if allowed_payment_method_types:
        params["allowed_payment_method_types"] = allowed_payment_method_types
    else:
        params["automatic_payment_methods"] = {"enabled": True}
    try:
        intent = client.v1.payment_intents.create(
            params=params,
            # Retrying the same checkout never creates a second PaymentIntent.
            options={"idempotency_key": idempotency_key},
        )
    except stripe.StripeError as exc:
        raise PaymentGatewayError(str(exc)) from exc
    return {"id": intent.id, "client_secret": intent.client_secret}


def parse_webhook(payload: bytes, signature_header: str | None, webhook_secret: str) -> dict[str, Any]:
    """Verify the Stripe-Signature header and return the event as a dict."""
    if not signature_header:
        raise InvalidWebhookError("missing Stripe-Signature header")
    try:
        event = stripe.Webhook.construct_event(payload, signature_header, webhook_secret)
    except stripe.SignatureVerificationError as exc:
        raise InvalidWebhookError("invalid signature") from exc
    except ValueError as exc:
        raise InvalidWebhookError("invalid payload") from exc
    return event.to_dict()
