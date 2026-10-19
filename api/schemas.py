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