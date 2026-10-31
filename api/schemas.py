from pydantic import BaseModel, Field
from typing import List, Optional

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


class MarketplaceListingRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=4000)
    category: str = Field(min_length=2, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=80)
    model: Optional[str] = Field(default=None, max_length=80)
    condition: str = Field(pattern="^(new|used|refurbished)$")
    price: float = Field(gt=0, le=100000)
    image_urls: List[str] = Field(min_length=1, max_length=6)
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
    condition: str
    type: str
    price: float
    image_urls: List[str] = Field(default_factory=list)
    seller_name: str
    partner_name: Optional[str] = None
    sponsored: bool = False
    status: str