from pydantic import BaseModel, validator, Field
from typing import Optional

class CreateListing(BaseModel):
    business_name: str
    category: str
    latitude: float
    longitude: float
    listing_type: Optional[str] = 'business'
    city: Optional[str] = ''
    state: Optional[str] = ''
    description: Optional[str] = ''
    phone: Optional[str] = ''
    whatsapp: Optional[str] = ''
    website: Optional[str] = ''

    @validator('latitude')
    def check_lat(cls, v):
        if not -90 <= v <= 90:
            raise ValueError('Latitude must be between -90 and 90')
        return v

    @validator('longitude')
    def check_lng(cls, v):
        if not -180 <= v <= 180:
            raise ValueError('Longitude must be between -180 and 180')
        return v

class UpdateListing(BaseModel):
    business_name: Optional[str] = None
    category: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    description: Optional[str] = None

class AddReview(BaseModel):
    listing_id: int
    rating: int = Field(..., ge=1, le=5)
    review: Optional[str] = ''

class BrowseQuery(BaseModel):
    page: int = 1
    search: Optional[str] = ''
    category: Optional[str] = ''
    location: Optional[str] = ''
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance: Optional[float] = None