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

    # Bid / cost (captured from GraphQL response during Playwright load)
    lot_id: Optional[str] = None  # internal UUID — used for bid matching
    current_bid_eur: Optional[float] = None
    buyer_premium_pct: Optional[float] = None
    total_cost_eur: Optional[float] = None  # bid + premium + VAT — all-in cost
    bids_count: Optional[int] = None
    reserve_met: Optional[bool] = None
    auction_end: Optional[datetime] = None

    # Market reference (from Marktplaats retail index)
    market_median_eur: Optional[float] = None
    market_sample_size: Optional[int] = None
    deal_margin_eur: Optional[float] = None  # market_median - total_cost
    deal_margin_pct: Optional[float] = None  # deal_margin / market_median * 100

    # Intelligence layer (Phase 2)
    size_class: Optional[str] = None  # L3H2, L2H2, H2+, etc — whatever the detector confirmed
    passed_hard_filters: bool = False
    rejected_reason: Optional[str] = None
    scores: Optional[ScoreBreakdown] = None
    total_score: Optional[float] = None
    reason_for_inclusion: Optional[List[str]] = None
