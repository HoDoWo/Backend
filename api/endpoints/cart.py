"""Marketplace cart endpoints (#42). Every response is the whole updated cart."""

from typing import Any

import cart_service
import fastapi
import schemas
from endpoints import checkout

cart_router = fastapi.APIRouter()


@cart_router.get("/api/marketplace/cart", response_model=dict)
async def get_cart(request: fastapi.Request) -> dict[str, Any]:
    """Return the user's cart with current prices and server-side totals."""
    return checkout._run(cart_service.get_cart, checkout._uid(request))


@cart_router.post("/api/marketplace/cart/items", response_model=dict)
async def add_item(req: schemas.CartAddRequest, request: fastapi.Request) -> dict[str, Any]:
    """Add a product, or increase its quantity when it is already in the cart."""
    return checkout._run(
        cart_service.add_item, checkout._uid(request), req.product_type, req.product_id, req.quantity
    )


@cart_router.patch("/api/marketplace/cart/items/{cart_item_id}", response_model=dict)
async def set_quantity(
    cart_item_id: str, req: schemas.CartQuantityRequest, request: fastapi.Request
) -> dict[str, Any]:
    """Change the quantity of one of the user's cart lines."""
    return checkout._run(cart_service.set_quantity, checkout._uid(request), cart_item_id, req.quantity)


@cart_router.delete("/api/marketplace/cart/items/{cart_item_id}", response_model=dict)
async def remove_item(cart_item_id: str, request: fastapi.Request) -> dict[str, Any]:
    """Remove one of the user's cart lines."""
    return checkout._run(cart_service.remove_item, checkout._uid(request), cart_item_id)
