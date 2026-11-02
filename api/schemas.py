from pydantic import BaseModel, Field
from typing import List, Literal, Optional

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
