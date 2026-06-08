"""Multi-source retail market price index — rotating per-source cache.

Each run refreshes exactly ONE price source (the stalest one) and merges it
into a per-source cache at ``output/price_cache.json``. The PriceIndex is
always built from all cached sources combined.

With 4 sources and a 6h GH Actions cron, every source is refreshed within 24h.
Each run saves ~10-15 min compared to rebuilding everything every time.

Cache layout:
    {
      "marktplaats":  {"updated_at": "...", "listings": [...]},
      "autoscout24":  {"updated_at": "...", "listings": [...]},
      "gaspedaal":    {"updated_at": "...", "listings": [...]},
      "2dehands":     {"updated_at": "...", "listings": [...]}
    }
"""

import json
import os
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional

import autoscout24
import autotrack
import gaspedaal
import marktplaats
import two_dehands

CACHE_PATH = "output/price_cache.json"

_MP_QUERIES = [
    # whitelist canonical names (camper-candidate models)
    "Ford Transit Custom", "Ford Tourneo Custom",
    "Peugeot Expert", "Citroen Jumpy", "Toyota ProAce",
    "Fiat Scudo",
    "Opel Vivaro", "Renault Trafic", "Nissan Primastar", "Fiat Talento",
    "Volkswagen Transporter",
    "Peugeot Boxer", "Citroen Jumper", "Fiat Ducato",
    "Mercedes Vito", "Mercedes V-Klasse",
    "Hyundai Staria",
    # legacy big-van queries — kept because they pad the dataset cheaply
    # and the auction-side PriceIndex still uses them for fallback medians.
    "Mercedes Sprinter", "Ford Transit", "Renault Master",
    "Volkswagen Crafter", "Opel Movano", "MAN TGE", "Iveco Daily",
]


class PriceIndex:
    """(model_key, year) → pooled EUR asking prices from all sources."""

    def __init__(self, listings: List[dict]):
        self._data: Dict[tuple, List[float]] = {}
        self._sources: Dict[tuple, set] = {}
        for item in listings:
            mk = item.get("model_key")
            yr = item.get("year")
            price = item.get("price_eur")
            src = item.get("source", "unknown")
            if mk and yr and price:
                key = (mk, int(yr))
                self._data.setdefault(key, []).append(float(price))
                self._sources.setdefault(key, set()).add(src)

    def median(self, model_key: Optional[str], year: Optional[int]) -> Optional[float]:
        """Median retail asking price within ±2 years. None if < 3 data points."""
        if not model_key or not year:
            return None
        prices: List[float] = []
        for dy in range(-2, 3):
            prices.extend(self._data.get((model_key, year + dy), []))
        if len(prices) < 3:
            return None
        return round(statistics.median(prices))

    def sample_size(self, model_key: Optional[str], year: Optional[int]) -> int:
        if not model_key or not year:
            return 0
        total = 0
        for dy in range(-2, 3):
            total += len(self._data.get((model_key, year + dy), []))
        return total

    def sources(self, model_key: Optional[str], year: Optional[int]) -> set:
        if not model_key or not year:
            return set()
        result: set = set()
        for dy in range(-2, 3):
            result |= self._sources.get((model_key, year + dy), set())
        return result


# ---------------------------------------------------------------------------
# Per-source fetchers
# ---------------------------------------------------------------------------

def _fetch_marktplaats(pages: int = 3) -> List[dict]:
    all_listings: List[dict] = []
    print("Refreshing Marktplaats...")
    for q in _MP_QUERIES:
        print(f"  marktplaats: {q} ...", end=" ", flush=True)
        listings = marktplaats.fetch_market_prices(q, pages=pages)
        for item in listings:
            item.setdefault("source", "marktplaats")
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    # Enrich whitelist-candidate listings with VIP-page seats so 3-seat
    # cargo vans get caught by strict_filter downstream. Capped at 200 per
    # run (~50s) so a Marktplaats refresh day doesn't push the GH Actions
    # total over 60 min. Seat data persists in price_cache.json across runs,
    # so coverage builds up incrementally (~11 Marktplaats cycles per week
    # covers all ~2200 whitelist-keyed listings within a week).
    marktplaats.enrich_listings_with_seats(all_listings, max_fetches=200)
    return all_listings


def _fetch_autoscout24(pages: int = 4) -> List[dict]:
    print("Refreshing AutoScout24...")
    keys = list(autoscout24._MODEL_SLUGS.keys())
    return autoscout24.build_listings(keys, pages_per_model=pages)


def _fetch_gaspedaal(pages: int = 5) -> List[dict]:
    print("Refreshing Gaspedaal...")
    try:
        keys = list(gaspedaal._MODEL_SLUGS.keys())
        return gaspedaal.build_listings(keys, pages_per_model=pages)
    except Exception as e:
        print(f"  gaspedaal failed: {e}")
        return []


def _fetch_2dehands(pages: int = 3) -> List[dict]:
    print("Refreshing 2dehands.be...")
    try:
        return two_dehands.build_listings(pages_per_query=pages)
    except Exception as e:
        print(f"  2dehands failed: {e}")
        return []


def _fetch_autotrack(pages: int = 4) -> List[dict]:
    print("Refreshing AutoTrack...")
    try:
        keys = list(autotrack._MODEL_SLUGS.keys())
        return autotrack.build_listings(keys, pages_per_model=pages)
    except Exception as e:
        print(f"  autotrack failed: {e}")
        return []


_SOURCES = {
    "marktplaats": _fetch_marktplaats,
    "autoscout24": _fetch_autoscout24,
    "gaspedaal":   _fetch_gaspedaal,
    "2dehands":    _fetch_2dehands,
    "autotrack":   _fetch_autotrack,
}


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache(path: str = CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cache, f)


def _stalest_source(cache: dict) -> str:
    """Return the name of the source with the oldest (or missing) update."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    def age(name: str) -> float:
        ts = cache.get(name, {}).get("updated_at")
        if not ts:
            return float("inf")
        try:
            dt = datetime.fromisoformat(ts)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            return float("inf")
    return max(_SOURCES, key=age)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_price_index_cached(path: str = CACHE_PATH) -> PriceIndex:
    """Refresh the stalest price source, then build a PriceIndex from all cached data.

    One source is fetched per call. Over 4 runs every source stays within 24h
    of freshness at the 6h GH Actions cadence."""
    cache = _load_cache(path)

    # Pick and refresh the stalest source
    source_name = _stalest_source(cache)
    fetcher = _SOURCES[source_name]
    listings = fetcher()

    cache[source_name] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "listings": listings,
    }
    _save_cache(cache, path)

    ages = {
        name: f"{(datetime.now(timezone.utc) - datetime.fromisoformat(cache[name]['updated_at'])).total_seconds()/3600:.0f}h"
        if name in cache else "never"
        for name in _SOURCES
    }
    total = sum(len(cache[n].get("listings", [])) for n in _SOURCES if n in cache)
    print(f"  price cache: refreshed={source_name} ages={ages} total={total} listings")

    # Build PriceIndex from all cached sources
    all_listings: List[dict] = []
    for name in _SOURCES:
        all_listings.extend(cache.get(name, {}).get("listings", []))
    return PriceIndex(all_listings)


# Legacy — kept so imports don't break; just calls the cached version
def build_price_index(**kwargs) -> PriceIndex:
    return build_price_index_cached()
