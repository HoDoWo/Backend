from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

class ManualStepSchema(BaseModel):
    number: int
    title: str
    description: str

class SaveManualRequest(BaseModel):
    appliance_name: Optional[str] = "Appliance"
    issue_title: str
    overview: str
    steps: List[ManualStepSchema]
    warnings: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    is_bookmarked: bool = False

class BookmarkRequest(BaseModel):
    is_bookmarked: bool

class RatingRequest(BaseModel):
    rating: int  # 1 Like, -1 Dislike


class CartAddRequest(BaseModel):
    product_type: Literal["new", "used", "part"]
    product_id: str = Field(min_length=1, max_length=128, pattern=r"^[^/]+$")
    quantity: int = Field(default=1, ge=1, le=99)


class CartQuantityRequest(BaseModel):
    quantity: int = Field(ge=1, le=99)


# Appliance forum (#44). Lengths are checked again after trimming, in forum_service.
class ForumThreadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: Optional[str] = Field(default=None, max_length=4000)
    # A category, or a product (its category is then the product's).
    category: Optional[str] = Field(default=None, max_length=40)
    appliance_model: Optional[str] = Field(default=None, max_length=160)
    product_type: Optional[Literal["new", "used", "part"]] = None
    product_id: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=r"^[^/]+$")


class ForumThreadEditRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=4000)


class ForumReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class ForumReportRequest(BaseModel):
    # "unsafe": repair advice that could hurt someone (mains power, gas...).
    reason: Literal["spam", "offensive", "unsafe", "off_topic", "other"]
    note: Optional[str] = Field(default=None, max_length=600)


class ForumModerationRequest(BaseModel):
    action: Literal["hide", "restore"]


class MarketplaceListingRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=30, max_length=800)
    category: str = Field(min_length=2, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=80)
    model: Optional[str] = Field(default=None, max_length=80)
    part_number: Optional[str] = Field(default=None, max_length=80)
    listing_kind: str = Field(pattern="^(appliance|part)$")
    condition: str = Field(pattern="^(unopened|like-new|used-good|parts-only|refurbished)$")
    price: float = Field(gt=0, le=100000)
    image_urls: List[str] = Field(min_length=1, max_length=8)
    diagnostic_media_urls: List[str] = Field(default_factory=list, max_length=8)
    handover_methods: List[str] = Field(min_length=1, max_length=2)
    pickup_zip: Optional[str] = Field(default=None, max_length=20)
    shipping_price_cents: Optional[int] = Field(default=None, ge=0, le=100000)
    quantity: int = Field(default=1, ge=1, le=99)


# Official partner products (#39). Unknown fields are refused, so a client
# cannot send partner_tier, partner_name or created_by.
PartnerProductCategory = Literal[
    "washing machine", "dryer", "dishwasher", "refrigerator", "oven", "hob", "microwave", "coffee machine",
]


class PartnerProductCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=3, max_length=120)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    category: PartnerProductCategory
    description: str = Field(default="", max_length=800)
    price: Decimal = Field(gt=0, le=100000, decimal_places=2)
    stock: int = Field(ge=0, le=100000)
    image_url: Optional[str] = Field(default=None, max_length=500)
    listing_kind: Literal["appliance", "part"] = "appliance"
    sponsored: Optional[bool] = None
    # Admins only: the partner the product belongs to.
    partner_id: Optional[str] = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")


class PartnerProductUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=120)
    brand: Optional[str] = Field(default=None, min_length=1, max_length=80)
    model: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[PartnerProductCategory] = None
    description: Optional[str] = Field(default=None, max_length=800)
    price: Optional[Decimal] = Field(default=None, gt=0, le=100000, decimal_places=2)
    stock: Optional[int] = Field(default=None, ge=0, le=100000)
    image_url: Optional[str] = Field(default=None, max_length=500)
    listing_kind: Optional[Literal["appliance", "part"]] = None
    sponsored: Optional[bool] = None


class MarketplaceListingUpdateRequest(BaseModel):
    """Owner edit of a listing (#35). ``condition`` also accepts the app's labels ("Like new")."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, min_length=30, max_length=800)
    category: Optional[str] = Field(default=None, min_length=2, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=80)
    model: Optional[str] = Field(default=None, max_length=80)
    part_number: Optional[str] = Field(default=None, max_length=80)
    listing_kind: Optional[Literal["appliance", "part"]] = None
    condition: Optional[str] = Field(default=None, min_length=3, max_length=30)
    price: Optional[float] = Field(default=None, gt=0, le=100000)
    image_urls: Optional[List[str]] = Field(default=None, min_length=1, max_length=8)
    handover_methods: Optional[List[Literal["pickup", "shipping"]]] = Field(default=None, min_length=1, max_length=2)
    pickup_zip: Optional[str] = Field(default=None, max_length=20)
    shipping_price_cents: Optional[int] = Field(default=None, ge=0, le=100000)
    quantity: Optional[int] = Field(default=None, ge=1, le=99)

DeliveryMethod = Literal["standard", "express", "pickup"]


class CartSelectionRequest(BaseModel):
    """What the buyer wants to buy now and how: used by the quote and the checkout."""

    model_config = ConfigDict(extra="forbid")

    # Cart lines to buy now (all of them when omitted); the others stay in the cart.
    cart_item_ids: Optional[List[str]] = Field(default=None, max_length=50)
    # seller_id -> delivery method for that seller's group.
    delivery: dict[str, DeliveryMethod] = Field(default_factory=dict, max_length=50)
    payment_method: Optional[Literal["card", "google_pay", "klarna"]] = None


class MarketplaceSearchResponse(BaseModel):
    items: List[dict]
    next_cursor: Optional[str] = None


class MarketplaceListingResponse(BaseModel):
    id: str
    title: str
    description: str
    category: str
    brand: Optional[str] = None
    model: Optional[str] = None
    part_number: Optional[str] = None
    listing_kind: str
    condition: str
    type: str
    price: float
    image_urls: List[str] = Field(default_factory=list)
    seller_name: str
    partner_name: Optional[str] = None
    sponsored: bool = False
    status: str
    handover_methods: List[str] = Field(default_factory=list)
    pickup_zip: Optional[str] = None
    shipping_price_cents: Optional[int] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    distance_km: Optional[float] = None
    warranty_info: Optional[str] = None
    original_price: Optional[float] = None
    discount_percent: Optional[int] = None
