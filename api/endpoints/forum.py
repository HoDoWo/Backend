"""Appliance forum endpoints (#44): categories, threads, replies, votes, reports.

Reads are open to every signed-in user; every write is rate limited per user.
"""

from typing import Annotated, Any, Literal, Optional

import config
import fastapi
import firebase_store
import forum_service
import logging_config
import schemas
from endpoints import checkout

logger = logging_config.get_logger(__name__)

forum_router = fastapi.APIRouter(prefix="/api/forum", tags=["forum"])

# Annotated types, not shared Path() defaults: FastAPI must see one Path per parameter.
ProductType = Literal["new", "used", "part"]
ProductId = Annotated[str, fastapi.Path(min_length=1, max_length=128, pattern=r"^[^/]+$")]
PostId = Annotated[str, fastapi.Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
CategoryId = Annotated[str, fastapi.Path(min_length=1, max_length=40, pattern=r"^[a-z-]+$")]
Cursor = Annotated[Optional[str], fastapi.Query(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Limit = Annotated[int, fastapi.Query(ge=1, le=forum_service.MAX_PAGE)]
Sort = Literal["recent", "popular"]

_UNAVAILABLE = (firebase_store.FirebaseConfigurationError, config.ConfigurationError)


def _run(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except forum_service.ForumError as exc:
        raise fastapi.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except _UNAVAILABLE as exc:
        logger.error("The forum is misconfigured: %s", exc)
        raise fastapi.HTTPException(status_code=503, detail="the forum is unavailable") from exc


def _uid(request: fastapi.Request) -> str:
    return checkout._uid(request)


# categories and lists

@forum_router.get("/categories", response_model=dict)
async def list_categories() -> dict[str, Any]:
    """The fixed list of appliance categories."""
    return forum_service.list_categories()


@forum_router.get("/categories/{category}/threads", response_model=dict)
async def list_category_threads(
    request: fastapi.Request, category: CategoryId, sort: Sort = "recent",
    limit: Limit = forum_service.DEFAULT_PAGE, cursor: Cursor = None,
) -> dict[str, Any]:
    """One page of a category's threads; send ``next_cursor`` back as ``cursor`` for the next."""
    return _run(forum_service.list_category_threads, _uid(request), category, sort, limit, cursor)


@forum_router.get("/products/{product_type}/{product_id}/threads", response_model=dict)
async def list_product_threads(
    request: fastapi.Request, product_type: ProductType, product_id: ProductId,
    sort: Sort = "popular", limit: Limit = forum_service.DEFAULT_PAGE,
) -> dict[str, Any]:
    """Questions about one product (detail page), each with its most upvoted reply."""
    return _run(forum_service.list_product_threads, _uid(request), product_type, product_id, sort, limit)


# threads

@forum_router.post("/threads", response_model=dict, status_code=201)
@config.forum_rate_limit
async def create_thread(request: fastapi.Request, req: schemas.ForumThreadRequest) -> dict[str, Any]:
    return _run(
        forum_service.create_thread, request.state.user, _uid(request), req.title, req.body,
        req.category, req.appliance_model, req.product_type, req.product_id,
    )


@forum_router.get("/threads/{thread_id}", response_model=dict)
async def get_thread(request: fastapi.Request, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.get_thread, _uid(request), thread_id)


@forum_router.patch("/threads/{thread_id}", response_model=dict)
@config.forum_rate_limit
async def edit_thread(request: fastapi.Request, req: schemas.ForumThreadEditRequest, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.edit_thread, _uid(request), thread_id, req.title, req.body)


@forum_router.delete("/threads/{thread_id}", status_code=204)
@config.forum_rate_limit
async def delete_thread(request: fastapi.Request, thread_id: PostId) -> None:
    _run(forum_service.delete_post, _uid(request), thread_id)


# replies

@forum_router.get("/threads/{thread_id}/replies", response_model=dict)
async def list_replies(
    request: fastapi.Request, thread_id: PostId,
    limit: Limit = forum_service.DEFAULT_PAGE, cursor: Cursor = None,
) -> dict[str, Any]:
    """One page of replies, oldest first."""
    return _run(forum_service.list_replies, _uid(request), thread_id, limit, cursor)


@forum_router.post("/threads/{thread_id}/replies", response_model=dict, status_code=201)
@config.forum_rate_limit
async def create_reply(request: fastapi.Request, req: schemas.ForumReplyRequest, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.create_reply, request.state.user, _uid(request), thread_id, req.body)


@forum_router.patch("/threads/{thread_id}/replies/{reply_id}", response_model=dict)
@config.forum_rate_limit
async def edit_reply(
    request: fastapi.Request, req: schemas.ForumReplyRequest, thread_id: PostId, reply_id: PostId
) -> dict[str, Any]:
    return _run(forum_service.edit_reply, _uid(request), thread_id, reply_id, req.body)


@forum_router.delete("/threads/{thread_id}/replies/{reply_id}", status_code=204)
@config.forum_rate_limit
async def delete_reply(request: fastapi.Request, thread_id: PostId, reply_id: PostId) -> None:
    _run(forum_service.delete_post, _uid(request), thread_id, reply_id)


# votes: PUT adds the user's upvote, DELETE withdraws it (both idempotent)

@forum_router.put("/threads/{thread_id}/vote", response_model=dict)
@config.forum_rate_limit
async def upvote_thread(request: fastapi.Request, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.vote, _uid(request), thread_id, None, up=True)


@forum_router.delete("/threads/{thread_id}/vote", response_model=dict)
@config.forum_rate_limit
async def unvote_thread(request: fastapi.Request, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.vote, _uid(request), thread_id, None, up=False)


@forum_router.put("/threads/{thread_id}/replies/{reply_id}/vote", response_model=dict)
@config.forum_rate_limit
async def upvote_reply(request: fastapi.Request, thread_id: PostId, reply_id: PostId) -> dict[str, Any]:
    return _run(forum_service.vote, _uid(request), thread_id, reply_id, up=True)


@forum_router.delete("/threads/{thread_id}/replies/{reply_id}/vote", response_model=dict)
@config.forum_rate_limit
async def unvote_reply(request: fastapi.Request, thread_id: PostId, reply_id: PostId) -> dict[str, Any]:
    return _run(forum_service.vote, _uid(request), thread_id, reply_id, up=False)


# reports and moderation

@forum_router.post("/threads/{thread_id}/report", response_model=dict, status_code=202)
@config.forum_rate_limit
async def report_thread(request: fastapi.Request, req: schemas.ForumReportRequest, thread_id: PostId) -> dict[str, Any]:
    return _run(forum_service.report, _uid(request), thread_id, None, req.reason, req.note)


@forum_router.post("/threads/{thread_id}/replies/{reply_id}/report", response_model=dict, status_code=202)
@config.forum_rate_limit
async def report_reply(
    request: fastapi.Request, req: schemas.ForumReportRequest, thread_id: PostId, reply_id: PostId
) -> dict[str, Any]:
    return _run(forum_service.report, _uid(request), thread_id, reply_id, req.reason, req.note)


@forum_router.post("/threads/{thread_id}/moderation", response_model=dict)
async def moderate_thread(
    request: fastapi.Request, req: schemas.ForumModerationRequest, thread_id: PostId
) -> dict[str, Any]:
    """Admin only (custom claim ``admin: true``): hide or restore a thread."""
    return _run(forum_service.moderate, request.state.user, thread_id, None, req.action)


@forum_router.post("/threads/{thread_id}/replies/{reply_id}/moderation", response_model=dict)
async def moderate_reply(
    request: fastapi.Request, req: schemas.ForumModerationRequest, thread_id: PostId, reply_id: PostId
) -> dict[str, Any]:
    """Admin only (custom claim ``admin: true``): hide or restore a reply."""
    return _run(forum_service.moderate, request.state.user, thread_id, reply_id, req.action)
