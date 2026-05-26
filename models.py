from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class ScoreBreakdown(BaseModel):
    geometry: int
    modularity: int
    conversion_friction: int
    mileage: int
    eu_usability: int


class Vehicle(BaseModel):
    # Identity
    title: str
    brand: Optional[str] = None
    model: Optional[str] = None
    url: str
    source: str = "troostwijk"
    platform: Optional[str] = None  # TWK, VAVATO, etc — Troostwijk sub-brand

    # Mechanical
    year: Optional[int] = None
    first_registration: Optional[date] = None
    km: Optional[int] = None
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    power_kw: Optional[int] = None
    cylinder_cc: Optional[int] = None
    emission_standard: Optional[str] = None

    # Body
    seats: Optional[int] = None
    doors: Optional[int] = None
    color: Optional[str] = None
    weight_kg: Optional[int] = None
    load_kg: Optional[int] = None
    vin: Optional[str] = None

    # Location
    city: Optional[str] = None
    country_code: Optional[str] = None
    location: Optional[str] = None  # "city, COUNTRY" formatted

    # Auction
    condition: Optional[str] = None  # WORKING / NOT_CHECKED / ...
    appearance: Optional[str] = None
    vat_margin: Optional[bool] = None  # marginGood — true = VAT margin scheme
    bidding_status: Optional[str] = None
    auction_start: Optional[datetime] = None

    # Free text (defect descriptions often live here)
    remarks: Optional[str] = None
    additional_information: Optional[str] = None

    # Intelligence layer (Phase 2)
    size_class: Optional[str] = None  # L3H2, L2H2, H2+, etc — whatever the detector confirmed
    passed_hard_filters: bool = False
    rejected_reason: Optional[str] = None
    scores: Optional[ScoreBreakdown] = None
    total_score: Optional[float] = None
    reason_for_inclusion: Optional[List[str]] = None
