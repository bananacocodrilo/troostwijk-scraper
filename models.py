from pydantic import BaseModel
from typing import Optional


class Vehicle(BaseModel):
    title: str
    brand: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    km: Optional[int] = None
    fuel: Optional[str] = None
    location: Optional[str] = None
    url: str
    source: str = "troostwijk"

    van_type: Optional[str] = None
    is_valid_van: bool = False
    score: int = 0
    hidden_gem: bool = False
