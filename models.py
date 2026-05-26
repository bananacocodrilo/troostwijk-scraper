from pydantic import BaseModel
from typing import Optional

class Vehicle(BaseModel):
    title: str
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    km: Optional[int] = None
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    location: Optional[str] = None
    url: str
    auction_end: Optional[str] = None
    source: str = "troostwijk"
