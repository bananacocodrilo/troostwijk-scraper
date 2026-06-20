"""Asking-price aggregator feed.

Parallel pipeline to the auction-side scraper: reuses the cached
marketplace listings already pulled by ``market_price.py`` and runs each
one through the whitelist classifier + soft-gate strict filter + small-van
suitability scoring. Emits a deduplicated cross-source feed at
``output/asking_listings.json`` for the ``docs/asking.html`` dashboard.

No new HTTP work — the cache at ``output/price_cache.json`` is the only
input. Sources are rotationally refreshed once per ``run.py`` call, so
the asking feed is at most one cron cycle (~6 h) stale relative to the
underlying sites.
"""

import json
import os
import statistics
from typing import Dict, List, Optional, Tuple

from cost_model import compute_conversion_cost
import registry
from van_intel import (
    HARD_REJECT_BODY, HARD_REJECT_DAMAGE, HARD_REJECT_TYPE,
    _check_list, classify_vehicle, is_high_roof, score_small_van, strict_filter,
)
import risk

# Hard-reject patterns that apply to asking-price listings.
# We apply HARD_REJECT_BODY and HARD_REJECT_DAMAGE in full.
# HARD_REJECT_TYPE is applied minus the bare \bbus\b token, which fires
# on legitimate "Jumpy Bus" / "Talento Bus" passenger-trim names.
_ASKING_HARD_REJECT_BODY   = HARD_REJECT_BODY
_ASKING_HARD_REJECT_DAMAGE = HARD_REJECT_DAMAGE
_ASKING_HARD_REJECT_TYPE   = [
    (pat, label) for pat, label in HARD_REJECT_TYPE
    if label not in ("bus / coach",)   # "bus" hits Jumpy Bus, Talento Bus
]

# ---------------------------------------------------------------------------
# Source metadata
# ---------------------------------------------------------------------------

# URL host prefix per source (marketplace modules emit relative paths).
# autotrack already emits absolute URLs, so its prefix is empty — it's
# only listed here so _to_vehicle() doesn't drop the source.
# Gaspedaal isn't included — its parser doesn't preserve a listing URL
# and the asking feed has no usable target for the card link, so
# gaspedaal entries are dropped before dedup.
_HOST_PREFIX: Dict[str, str] = {
    "marktplaats":      "https://www.marktplaats.nl",
    "2dehands":         "https://www.2dehands.be",
    "autoscout24_nl":   "https://www.autoscout24.nl",
    "autoscout24_de":   "https://www.autoscout24.de",
    "autotrack":        "",
    # German + Dutch-lease sources all emit absolute URLs already.
    "kleinanzeigen_de": "",
    "mobile_de":        "",
    "regeljelease":     "",
    "financiallease":   "",
    "rosfinance":       "",
}

# Country per source — listings may already carry a "country" field (the new
# sources do); this is the fallback for the legacy NL/BE sources.
_SOURCE_COUNTRY: Dict[str, str] = {
    "marktplaats":      "nl",
    "autotrack":        "nl",
    "gaspedaal":        "nl",
    "autoscout24_nl":   "nl",
    "2dehands":         "be",
    "autoscout24_de":   "de",
    "kleinanzeigen_de": "de",
    "mobile_de":        "de",
    "regeljelease":     "nl",
    "financiallease":   "nl",
    "rosfinance":       "nl",
}

# Preference order when the same physical vehicle appears on multiple sites.
# Direct C2C/dealer marketplaces beat lease aggregators (gaspedaal is dropped).
_SOURCE_RANK: Dict[str, int] = {
    "marktplaats":      0,
    "2dehands":         1,
    "autotrack":        2,
    "autoscout24_nl":   3,
    "autoscout24_de":   4,
    "kleinanzeigen_de": 5,
    "mobile_de":        6,
    "regeljelease":     7,
    "financiallease":   8,
    "rosfinance":       9,
}


def _market(country: Optional[str]) -> str:
    """Cohort market bucket. DE prices run well below NL/BE, so German
    listings are compared within their own market; NL and BE (Benelux,
    similar pricing) are pooled together as 'nl'."""
    return "de" if country == "de" else "nl"

# Underpriced flag thresholds
_UNDERPRICED_PCT  = -15   # at least 15% below cohort median
_MIN_COHORT_SIZE  = 5     # require ≥5 listings in the (group, year±2) cohort


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache_listings(path: str) -> List[dict]:
    try:
        with open(path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: List[dict] = []
    for source_block in cache.values():
        out.extend(source_block.get("listings", []))
    return out


# ---------------------------------------------------------------------------
# Adapter: source listing → Vehicle-shape dict
# ---------------------------------------------------------------------------

def _to_vehicle(listing: dict) -> Optional[dict]:
    """Wrap a marketplace listing in the dict shape ``classify_vehicle`` /
    ``strict_filter`` / ``score_small_van`` expect. Returns None for
    entries that lack the minimum data (no URL or no title)."""
    source = listing.get("source") or ""
    if source.startswith("gaspedaal"):
        return None
    prefix = _HOST_PREFIX.get(source)
    if prefix is None:
        return None
    raw_url = listing.get("url") or ""
    if not raw_url:
        return None
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        url = raw_url
    else:
        url = prefix + raw_url

    title = listing.get("title") or ""
    if not title:
        return None

    return {
        "title":     title,
        "url":       url,
        "source":    source,
        "country":   listing.get("country") or _SOURCE_COUNTRY.get(source) or "nl",
        "year":      listing.get("year"),
        "km":        listing.get("km"),
        "price_eur": listing.get("price_eur"),
        "seats":              listing.get("seats"),
        "emission_standard":  None,
        # Infer body_type from URL category:
        # - Marktplaats: /auto-s/bestelauto-s/ → cargo van
        # - 2dehands: /auto-s/bestelwagens-en-lichte-vracht/ → cargo van
        # strict_filter uses this + crew-cab check to gate seat inference.
        "body_type":          (
            "bestelwagen"
            if ("bestelauto" in url or "bestelwagens" in url)
            else listing.get("body_type")
        ),
        "weight_kg":          None,
        # Description / seller free-text — stored as `remarks` so that
        # strict_filter and score_small_van see crew-cab/seat signals that
        # the seller put in the description rather than the title.
        "remarks":            listing.get("description") or "",
        "images":             listing.get("images") or [],
    }


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _dedupe_key(v: dict) -> Optional[tuple]:
    """Group identical vehicles cross-listed on multiple sites.

    Same vehicle = same model_group, same market, same year, ±2k km, ±€500
    price. Market is included so a NL and a DE listing at the same price are
    never merged. Returns None when any required dimension is missing — those
    entries bypass dedup (kept as-is)."""
    group = v.get("model_group")
    year  = v.get("year")
    km    = v.get("km")
    price = v.get("price_eur")
    if not group or year is None or km is None or price is None:
        return None
    return (group, _market(v.get("country")), int(year), int(km) // 2000, int(price) // 500)


def _dedupe(vehicles: List[dict]) -> List[dict]:
    best: Dict[tuple, dict] = {}
    no_key: List[dict] = []
    for v in vehicles:
        k = _dedupe_key(v)
        if k is None:
            no_key.append(v)
            continue
        cur = best.get(k)
        if cur is None or _SOURCE_RANK.get(v["source"], 99) < _SOURCE_RANK.get(cur["source"], 99):
            best[k] = v
    return list(best.values()) + no_key


# ---------------------------------------------------------------------------
# Cohort median / percentile
# ---------------------------------------------------------------------------

def _bucket(vehicles: List[dict]) -> Dict[Tuple[str, str, int], List[float]]:
    """Return {(model_group, market, year) → list of prices}, pooled across
    ±2 years downstream via _cohort_prices(). Market (de vs nl/be) is part of
    the key so 'underpriced' is judged within the same market."""
    out: Dict[Tuple[str, str, int], List[float]] = {}
    for v in vehicles:
        g = v.get("model_group")
        y = v.get("year")
        p = v.get("price_eur")
        if g and y is not None and p is not None:
            out.setdefault((g, _market(v.get("country")), int(y)), []).append(float(p))
    return out


def _cohort_prices(buckets: Dict, group: str, market: str, year: int) -> List[float]:
    pool: List[float] = []
    for dy in range(-2, 3):
        pool.extend(buckets.get((group, market, year + dy), []))
    return pool


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_feed(price_cache_path: str = "output/price_cache.json") -> List[dict]:
    """Return the asking-price feed (a list of result dicts ready to write)."""
    raw_listings = _load_cache_listings(price_cache_path)
    survivors: List[dict] = []
    rejected_count = 0

    for listing in raw_listings:
        v = _to_vehicle(listing)
        if v is None:
            continue

        # Hard filters — scan title AND description (sellers often put
        # "koelwagen" or "bakwagen" in the body text rather than the title).
        haystack = " ".join(filter(None, [
            v.get("title"), v.get("remarks"),
        ])).lower()
        hard_reject = (
            _check_list(haystack, _ASKING_HARD_REJECT_BODY)
            or _check_list(haystack, _ASKING_HARD_REJECT_DAMAGE)
            or _check_list(haystack, _ASKING_HARD_REJECT_TYPE)
        )
        if hard_reject:
            rejected_count += 1
            continue

        cls = classify_vehicle(v["title"], v.get("remarks") or "")
        passed, _reason = strict_filter(v, cls)
        if not passed:
            rejected_count += 1
            continue

        v["model_group"]               = cls.group
        v["variant"]                   = cls.variant
        v["classification_confidence"] = cls.confidence
        v["matched_token"]             = cls.matched_token
        v["score"]                     = score_small_van(v)
        # Conversion cost + total project cost. For asking listings,
        # acquisition cost = asking price (no premium/VAT/transport math
        # to add — the listing price is what you pay private-party).
        v.update(compute_conversion_cost(v))
        # Risk metadata — marketplace listings only ship title (no
        # remarks / extras), so most flags won't fire. That's fine —
        # the structure is consistent and the dashboard handles
        # empty flag lists gracefully.
        v.update(risk.compute_risk(v))
        survivors.append(v)

    deduped = _dedupe(survivors)

    # Drop listings the user manually dismissed in the dashboard. Done after
    # dedupe (so a dismissed cross-listing can't resurface under another
    # source) and before cohort medians so dismissed prices don't skew them.
    dismissed = set(registry.load_user_overrides()["dismissed"].keys())
    dismissed_dropped = 0
    if dismissed:
        before = len(deduped)
        deduped = [v for v in deduped if v.get("url") not in dismissed]
        dismissed_dropped = before - len(deduped)

    buckets = _bucket(deduped)
    for v in deduped:
        g, y, p = v.get("model_group"), v.get("year"), v.get("price_eur")
        if not g or y is None or p is None:
            v["market_median_eur"]     = None
            v["market_sample_size"]    = 0
            v["price_pct_vs_median"]   = None
            v["is_underpriced"]        = False
            continue
        pool = _cohort_prices(buckets, g, _market(v.get("country")), int(y))
        n = len(pool)
        v["market_sample_size"] = n
        if n >= 3:
            med = round(statistics.median(pool))
            v["market_median_eur"]   = med
            v["price_pct_vs_median"] = round((p - med) / med * 100, 1)
            v["is_underpriced"]      = (
                v["price_pct_vs_median"] <= _UNDERPRICED_PCT
                and n >= _MIN_COHORT_SIZE
            )
        else:
            v["market_median_eur"]   = None
            v["price_pct_vs_median"] = None
            v["is_underpriced"]      = False

    # Sort: underpriced first, then by score desc, then by price asc.
    deduped.sort(key=lambda x: (
        not x.get("is_underpriced"),
        -(x.get("score") or 0),
        x.get("price_eur") or 0,
    ))

    print(
        f"  asking_feed: in={len(raw_listings)} "
        f"classified_pass={len(survivors)} "
        f"deduped={len(deduped)} "
        f"dismissed_dropped={dismissed_dropped} "
        f"rejected={rejected_count}"
    )
    return deduped


def write_feed(
    price_cache_path: str = "output/price_cache.json",
    out_path: str = "output/asking_listings.json",
    high_roof_path: str = "output/asking_l2h2.json",
) -> int:
    feed = build_feed(price_cache_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(feed, f, indent=2, default=str)
    # High-roof asking feed: only confirmed H2/H3 listings (no guessing).
    high = [v for v in feed if is_high_roof(v)]
    with open(high_roof_path, "w") as f:
        json.dump(high, f, indent=2, default=str)
    print(f"  asking_feed: wrote {len(feed)} listings ({len(high)} high-roof) ")
    return len(feed)


if __name__ == "__main__":
    n = write_feed()
    print(f"wrote {n} listings to output/asking_listings.json")
