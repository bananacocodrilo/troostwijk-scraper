"""Multi-source retail market price index.

Combines Marktplaats (C2C + dealer) and AutoScout24 (dealer-skewed) into a
single PriceIndex. Pooling both sources gives better coverage per
(model_key, year) bucket, especially for newer or less-common models.

Usage (mirrors the old marktplaats-only call in run.py):

    from market_price import build_price_index
    index = build_price_index(model_keys)
    median = index.median("boxer", 2019)
"""

import statistics
from typing import Dict, List, Optional

import autoscout24
import marktplaats


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
        """Which data sources contributed to this bucket (±2 years)."""
        if not model_key or not year:
            return set()
        result: set = set()
        for dy in range(-2, 3):
            result |= self._sources.get((model_key, year + dy), set())
        return result


def build_price_index(
    model_keys: Optional[List[str]] = None,
    *,
    marktplaats_queries: Optional[List[str]] = None,
    mp_pages: int = 3,
    as24_pages: int = 4,
    skip_autoscout24: bool = False,
) -> PriceIndex:
    """Build a combined PriceIndex from Marktplaats + AutoScout24.

    Args:
        model_keys: list of van_intel model tokens to fetch from AutoScout24
            (e.g. ["boxer", "ducato"]). Defaults to all known models.
        marktplaats_queries: human-readable search terms for Marktplaats
            (e.g. ["Peugeot Boxer", "Fiat Ducato"]). Defaults to all models.
        mp_pages: pages to fetch per Marktplaats query (100 listings/page).
        as24_pages: pages to fetch per AutoScout24 model (20 listings/page).
        skip_autoscout24: set True to fall back to Marktplaats-only (e.g. if
            AutoScout24 is temporarily blocking).
    """
    all_listings: List[dict] = []

    # --- Marktplaats ---
    _mp_queries = marktplaats_queries or [
        "Peugeot Boxer", "Citroen Jumper", "Fiat Ducato",
        "Mercedes Sprinter", "Ford Transit", "Renault Master",
        "Volkswagen Crafter", "Opel Movano", "MAN TGE", "Iveco Daily",
        "Peugeot Expert", "Volkswagen Transporter",
    ]
    print("Building Marktplaats price index...")
    for q in _mp_queries:
        print(f"  marktplaats: {q} ...", end=" ", flush=True)
        listings = marktplaats.fetch_market_prices(q, pages=mp_pages)
        # tag source so PriceIndex can attribute them
        for item in listings:
            item.setdefault("source", "marktplaats")
        print(f"{len(listings)} listings")
        all_listings.extend(listings)

    # --- AutoScout24 ---
    if not skip_autoscout24:
        print("Building AutoScout24 price index...")
        keys = model_keys or list(autoscout24._MODEL_SLUGS.keys())
        as24_listings = autoscout24.build_listings(keys, pages_per_model=as24_pages)
        all_listings.extend(as24_listings)

    return PriceIndex(all_listings)
