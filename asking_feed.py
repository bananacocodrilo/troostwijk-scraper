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

from van_intel import classify_vehicle, score_small_van, strict_filter
import risk

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
}

# Preference order when the same physical vehicle appears on multiple sites.
# Direct C2C/dealer sites beat aggregators (gaspedaal is already dropped).
_SOURCE_RANK: Dict[str, int] = {
    "marktplaats":     0,
    "2dehands":        1,
    "autotrack":       2,
    "autoscout24_nl":  3,
    "autoscout24_de":  4,
}

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
        "year":      listing.get("year"),
        "km":        listing.get("km"),
        "price_eur": listing.get("price_eur"),
        # Marketplace listings rarely expose these. AutoTrack ships
        # body_type (carrosserievormSlug) per hit; others leave it None.
        # Soft-gate handles None gracefully.
        "seats":              None,
        "emission_standard":  None,
        "body_type":          listing.get("body_type"),
        "weight_kg":          None,
    }


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _dedupe_key(v: dict) -> Optional[tuple]:
    """Group identical vehicles cross-listed on multiple sites.

    Same vehicle = same model_group, same year, ±2k km, ±€500 price.
    Returns None when any required dimension is missing — those entries
    bypass dedup (kept as-is)."""
    group = v.get("model_group")
    year  = v.get("year")
    km    = v.get("km")
    price = v.get("price_eur")
    if not group or year is None or km is None or price is None:
        return None
    return (group, int(year), int(km) // 2000, int(price) // 500)


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

def _bucket(vehicles: List[dict]) -> Dict[Tuple[str, int], List[float]]:
    """Return {(model_group, year) → sorted list of prices}, pooled across
    ±2 years downstream via _cohort_prices()."""
    out: Dict[Tuple[str, int], List[float]] = {}
    for v in vehicles:
        g = v.get("model_group")
        y = v.get("year")
        p = v.get("price_eur")
        if g and y is not None and p is not None:
            out.setdefault((g, int(y)), []).append(float(p))
    return out


def _cohort_prices(buckets: Dict, group: str, year: int) -> List[float]:
    pool: List[float] = []
    for dy in range(-2, 3):
        pool.extend(buckets.get((group, year + dy), []))
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

        cls = classify_vehicle(v["title"], "")
        passed, _reason = strict_filter(v, cls)
        if not passed:
            rejected_count += 1
            continue

        v["model_group"]               = cls.group
        v["variant"]                   = cls.variant
        v["classification_confidence"] = cls.confidence
        v["score"]                     = score_small_van(v)
        # Risk metadata — marketplace listings only ship title (no
        # remarks / extras), so most flags won't fire. That's fine —
        # the structure is consistent and the dashboard handles
        # empty flag lists gracefully.
        v.update(risk.compute_risk(v))
        survivors.append(v)

    deduped = _dedupe(survivors)

    buckets = _bucket(deduped)
    for v in deduped:
        g, y, p = v.get("model_group"), v.get("year"), v.get("price_eur")
        if not g or y is None or p is None:
            v["market_median_eur"]     = None
            v["market_sample_size"]    = 0
            v["price_pct_vs_median"]   = None
            v["is_underpriced"]        = False
            continue
        pool = _cohort_prices(buckets, g, int(y))
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
        f"rejected={rejected_count}"
    )
    return deduped


def write_feed(
    price_cache_path: str = "output/price_cache.json",
    out_path: str = "output/asking_listings.json",
) -> int:
    feed = build_feed(price_cache_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(feed, f, indent=2, default=str)
    return len(feed)


if __name__ == "__main__":
    n = write_feed()
    print(f"wrote {n} listings to output/asking_listings.json")
