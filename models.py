from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class ScoreBreakdown(BaseModel):
    year: int = 0
    mileage: int = 0
    van_size: int = 0
    emission: int = 0
    resaleability: int = 0
    crew_cab: int = 0

    def total(self) -> int:
        return min(
            self.year + self.mileage + self.van_size
            + self.emission + self.resaleability + self.crew_cab,
            100,
        )


class Vehicle(BaseModel):
    # Identity
    title: str
    brand: Optional[str] = None
    model: Optional[str] = None
    url: str
    source: str = "troostwijk"
    platform: Optional[str] = None
    thumbnail_url: Optional[str] = None

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
    location: Optional[str] = None

    # Auction
    condition: Optional[str] = None
    appearance: Optional[str] = None
    vat_margin: Optional[bool] = None
    bidding_status: Optional[str] = None
    auction_start: Optional[datetime] = None
    auction_end: Optional[datetime] = None

    # Free text
    remarks: Optional[str] = None
    additional_information: Optional[str] = None

    # Bid / cost
    lot_id: Optional[str] = None
    current_bid_eur: Optional[float] = None
    buyer_premium_pct: Optional[float] = None
    total_cost_eur: Optional[float] = None
    bids_count: Optional[int] = None
    reserve_met: Optional[bool] = None

    # Market reference (Marktplaats)
    market_median_eur: Optional[float] = None
    market_sample_size: Optional[int] = None
    deal_margin_eur: Optional[float] = None
    deal_margin_pct: Optional[float] = None
    max_recommended_bid_eur: Optional[float] = None

    # True acquisition cost model
    hammer_price: Optional[float] = None
    auction_fee_estimate: Optional[float] = None
    fixed_fees_estimate: Optional[float] = None
    vat_applicable: Optional[bool] = None
    transport_cost_estimate: Optional[int] = None
    reconditioning_cost_estimate: Optional[int] = None
    final_cost_estimate: Optional[int] = None
    estimated_market_value: Optional[int] = None
    deal_ratio: Optional[float] = None
    deal_score: Optional[int] = None       # 0-100, deal-ratio-derived
    is_hidden_gem: bool = False  # back-calc from market median

    # Intelligence layer (Phase 2+)
    van_type: Optional[str] = None        # detected size class: L3H2, L2H2, H2+, …
    is_valid_van: bool = False             # passed all hard filters
    score: int = 0                         # 0-100
    scores: Optional[ScoreBreakdown] = None
    applied_rule_set: Optional[str] = None
    passed_hard_filters: bool = False
    rejected_reason: Optional[str] = None
    reason_for_inclusion: Optional[List[str]] = None

    # Fleet provenance (informational — no score impact yet)
    fleet_type: Optional[str] = None       # utility | telecom | delivery | solar | …
    fleet_signals: Optional[List[str]] = None
