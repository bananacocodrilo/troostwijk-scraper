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

# Transport estimates by van size (€). Small-van L2 / L2H1 fits a
# regular open trailer; big high-roof L3H2+ needs a low-loader. High-roof
# (H2/H3) lots of unknown length default to a mid low-loader rate via the
# H2/H3 fallback in _transport_estimate().
_TRANSPORT = {
    "L2H1": 550, "L2H2": 650, "L2H?": 600, "L2": 550,
    "L4H3": 1300, "L3H3": 1200, "L3H2": 1100, "L4H2": 1200,
    "L2H3": 750, "L?H2": 700, "L?H3": 800,
    "H2+": 900, "H3": 1000, "L3": 1000, "panel": 700,
}
_TRANSPORT_DEFAULT = 600
_NON_RUNNING_SURCHARGE = 500

# Reconditioning estimates by condition bucket (€)
_RECON_GOOD    = 750    # WORKING, low km, recent year
_RECON_AVERAGE = 2000   # WORKING but older/higher km, or NOT_CHECKED
_RECON_POOR    = 4000   # explicit damage signals, very old/high km
_RECON_UNKNOWN = 3000   # no condition info

# Heuristic base market values at L2 baseline, by model group + year band.
# Used ONLY when Marktplaats median is unavailable (< 3 samples).
_BASE_PRICES = {
    # Small camper-candidate vans (Trafic / Vivaro / Primastar / Expert /
    # Jumpy / ProAce / Transit Custom / Tourneo Custom / Transporter T6.1
    # / Vito / Talento / Scudo gen3 / Staria).
    # Conservative asking-price medians; only used when market-data sources
    # return fewer than 3 samples. Added 2022 band for newer stock.
    "small_van":   {2022: 30_000, 2020: 24_000, 2017: 16_000, 2014: 10_000},
    # High-roof panel-van families (L2H2 pivot — now active whitelist groups).
    # Conservative L2H2 asking medians; only used when market-data sources
    # return fewer than 3 samples.
    #   premium → Sprinter / Crafter / TGE (best build, strongest resale)
    #   mid     → full-size Transit / Master / Movano / Interstar
    #   psa     → Ducato / Boxer / Jumper (Sevel large)
    "psa":         {2022: 26_000, 2020: 21_000, 2017: 15_000, 2014: 9_000},
    "premium":     {2022: 32_000, 2020: 26_000, 2017: 18_000, 2014: 11_000},
    "mid":         {2022: 28_000, 2020: 22_000, 2017: 15_000, 2014: 9_500},
}

_MODEL_GROUP = {
    # Small camper-candidate whitelist
    "trafic": "small_van", "vivaro": "small_van", "primastar": "small_van",
    "talento": "small_van",
    "expert": "small_van", "jumpy": "small_van", "proace": "small_van",
    "pro ace": "small_van", "scudo": "small_van",
    "transit custom": "small_van", "tourneo custom": "small_van",
    "transporter": "small_van", "t6.1": "small_van", "t6_1": "small_van",
    "vito": "small_van",
    "v-klasse": "small_van", "v klasse": "small_van", "v-class": "small_van",
    "staria": "small_van",
    # Sevel large (Ducato / Boxer / Jumper) — any height whitelisted.
    "boxer": "psa", "ducato": "psa", "jumper": "psa",
    # High-roof panel-van families (L2H2 pivot — now whitelisted).
    "sprinter": "premium", "crafter": "premium", "e-crafter": "premium", "tge": "premium",
    "transit": "mid", "master": "mid", "movano": "mid",
    "interstar": "mid", "nv400": "mid", "daily": "mid",
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
    vt = (van_type or "").upper()
    if vt in _TRANSPORT:
        return _TRANSPORT[vt]
    # Any other confirmed high roof (H2/H3) needs a low-loader, not the
    # small-van default — keeps big-van final_cost (and deal_ratio) honest.
    if "H3" in vt:
        return 900
    if "H2" in vt:
        return 800
    return _TRANSPORT_DEFAULT


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
    group = _MODEL_GROUP.get(model_token or "", "small_van")
    bases = _BASE_PRICES[group]

    if year is None:
        year = 2017

    if year >= 2022 and 2022 in bases:
        base = bases[2022]
    elif year >= 2020:
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

    # Size premium — L2H2 / L2 baseline; H1 slightly under, big high-roof
    # sizes over. Big-van high-roof families are first-class post-pivot, but
    # the premium/mid/psa _BASE_PRICES are already L2H2 medians, so the bumps
    # here apply mainly to legacy small-van bases scaled up to a long/high body.
    vt = (van_type or "").upper()
    if vt in ("L3H2", "L4H3", "L3H3", "L4H2"):
        base = int(base * 1.15)
    elif vt in ("H2+", "H3", "L3"):
        base = int(base * 1.10)
    elif vt in ("L2H2", "L2H?", "L2"):
        pass  # baseline
    elif vt in ("L2H1",):
        base = int(base * 0.95)
    elif vt in ("L1H1", "H1", "L1"):
        base = int(base * 0.85)

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
    # Camper-candidate sweet-spot. Matches the asking feed's scope (June 2026
    # L2H2 pivot): any CONFIRMED high roof (H2/H3 at ANY length — so big-van
    # L3H2/L3H3/L?H2 lots qualify, not just small L2 vans) PLUS the L2
    # small-van set (Transit Custom / Expert / Vito etc.). Unknown height in
    # the L2 set still passes (soft-gate); confirmed L1 / long low-roof do not.
    # km bar relaxed 150k→200k to match the asking dashboard default (≤200k).
    vt = (van_type or "").upper()
    L2_SWEET_SPOT = {"L2H1", "L2H2", "L2H?", "L2"}
    gem_size = ("H2" in vt or "H3" in vt) or vt in L2_SWEET_SPOT
    is_gem = bool(
        deal_ratio is not None and deal_ratio > 0.25
        and km is not None and km < 200_000
        and year is not None and year >= 2017
        and gem_size
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


# ---------------------------------------------------------------------------
# Conversion cost estimate
# ---------------------------------------------------------------------------
#
# Per-group bands for the cash needed to turn the base van into a usable
# weekender camper (insulation + windows + flooring + bed-kit + small
# storage + leisure electrics). Numbers are starting estimates from
# DIY camper-conversion price guides circa 2024-2025; tune later from
# real receipts. Detection re-uses regex from van_intel
# (_PASSENGER_TRIM_RE, _CREW_CAB_RE, plus the kombi/glazen-zij phrase).
#
#   minimal  — factory passenger trim (already insulated, windows fitted,
#              seats + climate + carpet trim installed). Just needs a bed
#              kit and a few storage bins. €1.5k–€3.5k.
#   light    — crew-cab / DC variants of cargo groups (front bench bolted
#              in, side window optional), OR cargo van that already has
#              side windows ("combi"/"kombi"/"glazen zij"). Saves the
#              window-cutting + half the insulation work. €3.5k–€8k.
#   moderate — plain cargo / panel van. Full conversion: cut and bond
#              windows, lay floor, insulate, panel out, fit seats,
#              kitchen, bed. €7k–€14k.
#   heavy    — moderate + confirmed low roof (H1). Same parts list, but
#              poor standing room drives most builders to add a pop-top
#              / elevating roof. +€1.5k/€3k on top of the moderate band
#              and bumps the public effort label one step.

# model_group keys that ship as factory passenger trim by default.
_PASSENGER_GROUPS = {
    "hyundai_staria",   # MPV — fully trimmed passenger van
    # vito_v_class_l2 contains BOTH Vito cargo and V-Class passenger,
    # so we use _PASSENGER_TRIM_RE on the title to disambiguate; do
    # NOT default the whole group to "minimal".
}

_EFFORT_ORDER = ["minimal", "light", "moderate", "heavy"]

# (low, high) band per effort label. "kombi" is an internal variant of
# "light" with a slightly higher upper bound (cargo + windows still
# needs more interior work than a factory passenger trim).
_CONVERSION_BANDS = {
    "minimal":  (1_500,  3_500),
    "light":    (3_500,  7_000),
    "kombi":    (4_000,  8_000),
    "moderate": (7_000, 14_000),
    "heavy":    (8_500, 17_000),
}


def _bump_effort(effort: str) -> str:
    """Move effort one step harder (capped at 'heavy')."""
    try:
        i = _EFFORT_ORDER.index(effort)
    except ValueError:
        return effort
    return _EFFORT_ORDER[min(i + 1, len(_EFFORT_ORDER) - 1)]


def compute_conversion_cost(vehicle: dict) -> dict:
    """Estimate the cash needed to convert the base van into a usable camper.

    Returns:
      - est_conversion_cost_eur:        midpoint of (low, high)
      - est_conversion_cost_low_eur
      - est_conversion_cost_high_eur
      - conversion_effort: 'minimal' | 'light' | 'moderate' | 'heavy'
      - total_project_cost_eur: best-available acquisition cost +
        est_conversion_cost_eur (priority: total_cost_eur →
        final_cost_estimate → max_recommended_bid_eur → price_eur for
        asking-feed listings). None when no acquisition figure exists.
    """
    # Local import avoids a top-level cycle with van_intel.
    from van_intel import _CREW_CAB_RE, _PASSENGER_TRIM_RE

    title    = vehicle.get("title") or ""
    remarks  = vehicle.get("remarks") or ""
    addl     = vehicle.get("additional_information") or ""
    hay      = f"{title} {remarks} {addl}".lower()

    group    = vehicle.get("model_group")
    van_type = (vehicle.get("van_type") or vehicle.get("variant") or "").upper()

    # 1) Base effort. Passenger trim wins outright; crew-cab beats
    #    kombi/combi (which beats plain cargo).
    if group in _PASSENGER_GROUPS or _PASSENGER_TRIM_RE.search(title):
        effort = "minimal"
    elif _CREW_CAB_RE.search(title):
        effort = "light"
    elif re.search(r"\b(kombi|combi|glazen\s*zij|side\s*window\s*van)\b", hay):
        effort = "kombi"
    else:
        effort = "moderate"

    low, high = _CONVERSION_BANDS[effort]

    # 2) Low-roof penalty (H1). Standing room matters for a usable
    #    camper. We skip the bump for "minimal" — factory passenger vans
    #    ship low-roof and that's accepted as part of their format.
    #    Pattern uses a negative lookahead instead of \b because \b
    #    doesn't fire between a digit and a letter (L2H1 has no boundary
    #    around H1).
    is_low_roof = bool(re.search(r"H1(?![0-9])", van_type))
    if effort != "minimal" and is_low_roof:
        low  += 1_500
        high += 3_000
        effort = _bump_effort(effort)

    # Map the internal "kombi" label to the public "light" tier; the
    # band difference is an implementation detail.
    public_effort = "light" if effort == "kombi" else effort

    mid = round((low + high) / 2)

    # 3) Project total = acquisition + conversion. Acquisition priority:
    #    real GraphQL total → fallback estimate → max bid → asking
    #    price (asking-feed listings have no auction context).
    acquisition = (
        vehicle.get("total_cost_eur")
        or vehicle.get("final_cost_estimate")
        or vehicle.get("max_recommended_bid_eur")
        or vehicle.get("price_eur")
    )
    total_project = (acquisition + mid) if acquisition is not None else None

    return {
        "est_conversion_cost_eur":      mid,
        "est_conversion_cost_low_eur":  low,
        "est_conversion_cost_high_eur": high,
        "conversion_effort":            public_effort,
        "total_project_cost_eur":       total_project,
    }


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
