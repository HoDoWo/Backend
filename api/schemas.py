from pydantic import BaseModel, Field
from typing import List, Optional, Literal

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
