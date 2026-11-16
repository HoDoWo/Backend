"""The user's notifications: unread messages, offers to answer, price changes in the cart and a sponsored product."""

from typing import Any

import fastapi
import notifications_service
from endpoints import conversations

notifications_router = fastapi.APIRouter(prefix="/api/notifications", tags=["notifications"])


@notifications_router.get("", response_model=dict)
async def list_notifications(request: fastapi.Request) -> dict[str, Any]:
    """Newest first, with how many are unread; needs a signed-in user."""
    return notifications_service.list_notifications(conversations._user(request))
