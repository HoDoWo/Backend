"""Buyer-seller conversations and offers on second-hand listings.

Every route needs a signed-in user; writes are rate limited per user.
"""

from typing import Annotated, Any

import auth
import config
import conversations_service
import fastapi
import firebase_store
import logging_config
import schemas

logger = logging_config.get_logger(__name__)

conversations_router = fastapi.APIRouter(prefix="/api/marketplace/conversations", tags=["conversations"])

ConversationId = Annotated[str, fastapi.Path(min_length=3, max_length=300, pattern=r"^[A-Za-z0-9_-]+$")]

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _user(request: fastapi.Request) -> dict[str, Any]:
    user = auth.get_current_user(request)
    uid = str(user.get("uid") or user.get("sub") or "").strip()
    if not uid:
        raise fastapi.HTTPException(status_code=401, detail="authenticated user id is missing")
    return {**user, "uid": uid}


def _run(action, *args):
    try:
        return action(*args)
    except conversations_service.ConversationError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("Messages are misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="messages are unavailable") from exc


@conversations_router.get("", response_model=dict)
async def list_conversations(request: fastapi.Request) -> dict[str, Any]:
    """The user's inbox, most recent first, with the total of unread messages."""
    return _run(conversations_service.list_conversations, _user(request)["uid"])


@conversations_router.post("", response_model=dict, status_code=201)
@config.messages_rate_limit
async def start_conversation(request: fastapi.Request, req: schemas.ConversationStartRequest) -> dict[str, Any]:
    """Open the conversation with the seller of a listing (the same one if it exists)."""
    return _run(conversations_service.start_conversation, _user(request), req.listing_id, req.text)


@conversations_router.get("/{conversation_id}", response_model=dict)
async def get_conversation(request: fastapi.Request, conversation_id: ConversationId) -> dict[str, Any]:
    """The conversation with its messages; marks it as read."""
    return _run(conversations_service.get_conversation, _user(request)["uid"], conversation_id)


@conversations_router.post("/{conversation_id}/messages", response_model=dict, status_code=201)
@config.messages_rate_limit
async def send_message(
    request: fastapi.Request, req: schemas.ConversationMessageRequest, conversation_id: ConversationId
) -> dict[str, Any]:
    return _run(conversations_service.send_message, _user(request)["uid"], conversation_id, req.text)


@conversations_router.post("/{conversation_id}/offers", response_model=dict, status_code=201)
@config.messages_rate_limit
async def make_offer(
    request: fastapi.Request, req: schemas.OfferRequest, conversation_id: ConversationId
) -> dict[str, Any]:
    """The buyer's offer, or the seller's counter-offer to a pending one."""
    return _run(conversations_service.make_offer, _user(request)["uid"], conversation_id, req.amount_cents)


@conversations_router.post("/{conversation_id}/offer/accept", response_model=dict)
@config.messages_rate_limit
async def accept_offer(request: fastapi.Request, conversation_id: ConversationId) -> dict[str, Any]:
    """Accept the other person's offer: the buyer can pay that price for 48 hours."""
    return _run(conversations_service.answer_offer, _user(request)["uid"], conversation_id, True)


@conversations_router.post("/{conversation_id}/offer/decline", response_model=dict)
@config.messages_rate_limit
async def decline_offer(request: fastapi.Request, conversation_id: ConversationId) -> dict[str, Any]:
    return _run(conversations_service.answer_offer, _user(request)["uid"], conversation_id, False)
