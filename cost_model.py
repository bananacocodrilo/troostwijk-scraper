"""True total acquisition cost model.

Computes what a private (non-VAT-deductible) buyer actually pays to own
a van after the auction, not just what they bid.

Total = auction_cost + fixed_fees + transport_estimate + reconditioning_estimate

auction_cost comes from Troostwijk GraphQL (accurate) or is estimated.
The rest is heuristic based on van size and condition signals.
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXED_FEES = 100            # registration, admin — flat estimate
DEFAULT_BUYER_PREMIUM = 0.19  # Troostwijk standard; overridden by real GraphQL value
VAT_RATE = 0.21             # Dutch BTW for private buyers on non-margin-scheme lots

# Transport estimates by van size (€)
_TRANSPORT = {
    "L4H3": 1300, "L3H3": 1200, "L3H2": 1100, "L4H2": 1200,
    "L2H2": 700,
    "H2+": 900, "H3": 1000, "L3": 1000, "panel": 700,
}
_TRANSPORT_DEFAULT = 600
_NON_RUNNING_SURCHARGE = 500

# Reconditioning estimates by condition bucket (€)
_RECON_GOOD    = 750    # WORKING, low km, recent year
_RECON_AVERAGE = 2000   # WORKING but older/higher km, or NOT_CHECKED
_RECON_POOR    = 4000   # explicit damage signals, very old/high km
_RECON_UNKNOWN = 3000   # no condition info

# Heuristic base market values at L2H2 baseline, by model group + year band.
# Used ONLY when Marktplaats median is unavailable (< 3 samples).
_BASE_PRICES = {
    # PSA triplets (Boxer/Jumper/Ducato) — cheaper, more common
    "psa": {2020: 18_000, 2017: 12_000, 2014: 7_500},
    # German premium (Sprinter, Crafter, TGE) — heavier duty, more expensive
    "premium": {2020: 25_000, 2017: 17_000, 2014: 11_000},
    # Mid-tier (Transit, Master, Movano, Daily)
    "mid": {2020: 20_000, 2017: 13_500, 2014: 9_000},
}

_MODEL_GROUP = {
    "boxer": "psa", "jumper": "psa", "ducato": "psa",
    "sprinter": "premium", "crafter": "premium", "tge": "premium",
    "transit": "mid", "master": "mid", "movano": "mid", "daily": "mid",
}

_DAMAGE_HINTS = re.compile(
    r"engine|motor|gearbox|versnelling|broken|defect|kapot|stuk|"
    r"not start|niet start|schade|damage|total loss|totalschade",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transport_estimate(van_type: Optional[str]) -> int:
    return _TRANSPORT.get((van_type or "").upper(), _TRANSPORT_DEFAULT)


def _recon_estimate(
    condition: Optional[str],
    km: Optional[int],
    year: Optional[int],
    remarks: Optional[str],
    additional_information: Optional[str],
) -> int:
    text = " ".join(s for s in [remarks, additional_information] if s)
    has_damage = bool(_DAMAGE_HINTS.search(text)) if text else False

    if has_damage:
        return _RECON_POOR

    if not condition:
        return _RECON_UNKNOWN

    if condition == "WORKING":
        good_km = km is None or km < 120_000
        good_year = year is None or year >= 2017
        if good_km and good_year:
            return _RECON_GOOD
        return _RECON_AVERAGE

    # NOT_CHECKED or anything else
    return _RECON_UNKNOWN


def _market_heuristic(
    model_token: Optional[str],
    year: Optional[int],
    km: Optional[int],
    van_type: Optional[str],
) -> Optional[int]:
    """Fallback market value when Marktplaats has insufficient samples."""
    group = _MODEL_GROUP.get(model_token or "", "mid")
    bases = _BASE_PRICES[group]

    if year is None:
        return None

    if year >= 2020:
        base = bases[2020]
    elif year >= 2017:
        base = bases[2017]
    elif year >= 2014:
        base = bases[2014]
    else:
        return None

    # Mileage depreciation
    if km is not None:
        if km >= 180_000:
            base = int(base * 0.75)
        elif km >= 100_000:
            base = int(base * 0.90)

    # Size premium
    vt = (van_type or "").upper()
    if vt in ("L3H2", "L4H3", "L3H3", "L4H2"):
        base = int(base * 1.15)
    elif vt in ("H2+", "H3", "L3"):
        base = int(base * 1.10)
    elif vt == "L2H2":
        pass  # baseline
    elif vt in ("L1H1", "H1", "L1"):
        base = int(base * 0.90)

    return base


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_costs(v: dict, model_token: Optional[str] = None) -> dict:
    """Compute all cost and deal fields. Returns a dict of new/updated fields
    to merge into the vehicle dict. Uses real GraphQL data where available."""

    hammer   = v.get("current_bid_eur")
    total_gql = v.get("total_cost_eur")   # from GraphQL: hammer + real premium + VAT
    premium_pct = v.get("buyer_premium_pct") or (DEFAULT_BUYER_PREMIUM * 100)
    vat_margin  = v.get("vat_margin")     # True = margin scheme (no 21% buyer VAT)
    condition   = v.get("condition")
    km          = v.get("km")
    year        = v.get("year")
    van_type    = v.get("van_type")
    remarks     = v.get("remarks")
    addl        = v.get("additional_information")
    market_med  = v.get("market_median_eur")
    market_n    = v.get("market_sample_size") or 0
    hammer_med  = v.get("hammer_median_eur")
    hammer_n    = v.get("hammer_sample_size") or 0

    # Auction cost ─────────────────────────────────────────────────────────
    auction_fee_estimate = None
    if hammer is not None:
        auction_fee_estimate = round(hammer * (premium_pct / 100))

    # Private buyer VAT: applies only on non-margin-scheme lots.
    vat_applicable = (vat_margin is False)

    # Preferred: real total from GraphQL (already includes actual premium + VAT).
    # Fallback: estimate.
    if total_gql is not None:
        auction_cost = total_gql
    elif hammer is not None:
        multiplier = (1 + premium_pct / 100)
        if vat_applicable:
            multiplier *= (1 + VAT_RATE)
        auction_cost = round(hammer * multiplier)
    else:
        auction_cost = None

    # Soft costs ───────────────────────────────────────────────────────────
    transport  = _transport_estimate(van_type)
    recon      = _recon_estimate(condition, km, year, remarks, addl)

    # Non-running surcharge
    text = " ".join(s for s in [remarks, addl] if s)
    if re.search(r"not start|niet start|non.?runner|non.?running", text or "", re.IGNORECASE):
        transport += _NON_RUNNING_SURCHARGE

    final_cost = (
        round(auction_cost + FIXED_FEES + transport + recon)
        if auction_cost is not None else None
    )

    # Market value ─────────────────────────────────────────────────────────
    # Priority: actual closed-auction hammer history (if we have enough
    # samples) → Marktplaats asking-price median → heuristic. Hammer
    # history reflects what real auction buyers paid, so it's the most
    # accurate reference once we've built up a usable dataset.
    if hammer_med and hammer_n >= 5:
        est_market = round(hammer_med)
        market_source = "hammer_history"
    elif market_med and market_n >= 3:
        est_market = round(market_med)
        market_source = "marktplaats"
    else:
        est_market = _market_heuristic(model_token, year, km, van_type)
        market_source = "heuristic"

    # Deal ratio ───────────────────────────────────────────────────────────
    deal_ratio = None
    if final_cost and est_market:
        deal_ratio = round((est_market - final_cost) / final_cost, 3)

    # Score from deal ratio ────────────────────────────────────────────────
    deal_score = _deal_score(deal_ratio)

    # Hidden gem ───────────────────────────────────────────────────────────
    is_gem = bool(
        deal_ratio is not None and deal_ratio > 0.25
        and km is not None and km < 150_000
        and year is not None and year >= 2017
        and (van_type or "").upper() in ("L3H2", "L2H2", "L4H3", "L3H3", "L4H2")
    )

    return {
        "hammer_price":              hammer,
        "auction_fee_estimate":      auction_fee_estimate,
        "fixed_fees_estimate":       FIXED_FEES,
        "vat_applicable":            vat_applicable,
        "transport_cost_estimate":   transport,
        "reconditioning_cost_estimate": recon,
        "final_cost_estimate":       final_cost,
        "estimated_market_value":    est_market,
        "market_value_source":       market_source,
        "deal_ratio":                deal_ratio,
        "deal_score":                deal_score,
        "is_hidden_gem":             is_gem,
    }


def _deal_score(deal_ratio: Optional[float]) -> Optional[int]:
    if deal_ratio is None:
        return None
    if deal_ratio > 0.30:
        return 95
    if deal_ratio > 0.15:
        return 82
    if deal_ratio > 0.05:
        return 67
    if deal_ratio >= 0:
        return 50
    if deal_ratio > -0.10:
        return 30
    return 15


def passes_cost_filter(v: dict) -> tuple[bool, Optional[str]]:
    """Return (passes, reason). Called after compute_costs is merged in."""
    final   = v.get("final_cost_estimate")
    market  = v.get("estimated_market_value")
    recon   = v.get("reconditioning_cost_estimate", 0)
    score   = v.get("score", 0)

    if final and market:
        if final > market * 1.05:
            return False, f"final_cost_above_market: €{final:,.0f} > €{market:,.0f}"

    # High recon penalty only for vans that aren't scoring as premium lots
    if recon >= 5000 and score < 70:
        return False, f"reconditioning_too_high: €{recon:,}"

    return True, None
