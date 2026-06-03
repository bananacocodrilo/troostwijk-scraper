"""Marktplaats retail price index.

Fetches asking prices for Ducato/Boxer/Jumper/Transit from the
Marktplaats JSON search API (no Playwright needed — plain HTTP works).
Used as a retail reference to compute deal margin against Troostwijk bids.
"""

import json
import re
import statistics
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

SEARCH_URL = "https://www.marktplaats.nl/lrp/api/search"
CATEGORY_ID = 571  # Vrachtwagens en campers (Trucks & Vans)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Referer": "https://www.marktplaats.nl/",
}

# Listings outside this range are outliers (parts cars, camper conversions, etc).
PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 45_000

# Marktplaats URL prefixes for categories that aren't actual vehicles.
# Branded queries (e.g. "Ford Tourneo Custom") return matches across the
# whole site, so we get parts (auto-onderdelen), accessories
# (camper-accessoires), and miscellaneous (auto-diversen) listings
# mixed in with the actual vans. Drop them at parse time so they never
# enter the cache or the asking feed.
_EXCLUDED_CATEGORY_PREFIXES = (
    "/v/auto-onderdelen/",
    "/v/auto-diversen/",
    "/v/caravans-en-kamperen/camper-accessoires/",
    "/v/caravans-en-kamperen/onderdelen-en-accessoires/",
)

# Model token → canonical key for bucketing. Multi-word tokens MUST come
# before single-word tokens (e.g. "transit custom" before "transit") so
# the substring match in _model_key() picks the more specific one first.
_MODEL_KEYS = {
    # whitelist multi-word — must precede their single-word substrings
    "transit custom":  "transit_custom",
    "tourneo custom":  "tourneo_custom",
    "v-klasse":        "vclass",
    "v klasse":        "vclass",
    "v-class":         "vclass",
    # whitelist single-word
    "boxer":     "boxer",
    "ducato":    "ducato",
    "jumper":    "jumper",
    "transit":   "transit",
    "expert":    "expert",
    "jumpy":     "jumpy",
    "proace":    "proace",
    "scudo":     "scudo",
    "vivaro":    "vivaro",
    "trafic":    "trafic",
    "primastar": "primastar",
    "talento":   "talento",
    "transporter": "transporter",
    "vito":      "vito",
    "staria":    "staria",
}


def _model_key(title: str) -> Optional[str]:
    s = title.lower()
    for token, key in _MODEL_KEYS.items():
        if token in s:
            return key
    return None


def _fetch_page(query: str, offset: int = 0) -> List[dict]:
    params = urllib.parse.urlencode({
        "query": query,
        "categoryId": CATEGORY_ID,
        "numberOfResultsPerPage": 100,
        "offset": offset,
    })
    req = urllib.request.Request(f"{SEARCH_URL}?{params}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("listings", [])
    except Exception as e:
        print(f"  marktplaats fetch error ({query} offset={offset}): {e}")
        return []


def _parse_listing(item: dict) -> Optional[dict]:
    price_info = item.get("priceInfo") or {}
    cents = price_info.get("priceCents")
    if not isinstance(cents, (int, float)):
        return None
    price_eur = cents / 100
    if not (PRICE_MIN_EUR <= price_eur <= PRICE_MAX_EUR):
        return None

    # Marktplaats URL path encodes the category. We only want actual
    # vehicles — drop car-parts ("auto-onderdelen"), car-misc
    # ("auto-diversen"), and camper-accessory ("camper-accessoires")
    # categories which leak in for branded-search queries.
    vip = item.get("vipUrl") or ""
    if vip.startswith(_EXCLUDED_CATEGORY_PREFIXES):
        return None

    attrs = {a["key"]: a.get("value") for a in (item.get("attributes") or [])}
    try:
        year = int(attrs.get("constructionYear") or 0) or None
    except (ValueError, TypeError):
        year = None
    try:
        km_raw = str(attrs.get("mileage") or "").replace(".", "").replace(",", "")
        km = int(km_raw) if km_raw.isdigit() else None
    except (ValueError, TypeError):
        km = None

    title = item.get("title") or ""
    # Up to 5 image URLs (mediumUrl preferred, fallback to largeUrl / url).
    images: List[str] = []
    for pic in (item.get("pictures") or []):
        if not isinstance(pic, dict):
            continue
        u = pic.get("mediumUrl") or pic.get("largeUrl") or pic.get("url")
        if isinstance(u, str) and u.startswith("http"):
            images.append(u)
        if len(images) >= 5:
            break
    return {
        "price_eur": price_eur,
        "year": year,
        "km": km,
        "title": title,
        "url": item.get("vipUrl") or "",
        "model_key": _model_key(title),
        "images": images,
        # seats populated by enrich_listings_with_seats() — the search
        # API doesn't expose it, only the per-listing VIP page does.
        "seats": None,
    }


# Marktplaats VIP (per-listing detail) page exposes the seat count in
# two places: the JSON-LD blob (`vehicleSeatingCapacity`) and the
# attribute table (`numberOfSeats` with Dutch label "Aantal stoelen").
# Either form yields the same integer — match either.
_VIP_SEATS_RE = re.compile(
    r'"numberOfSeats","label":"Aantal\s+stoelen","value":"(\d+)"'
    r'|"vehicleSeatingCapacity":"(\d+)"',
)
_VIP_BASE = "https://www.marktplaats.nl"


def _fetch_vip_seats(vip_url: str) -> Optional[int]:
    """Fetch one Marktplaats VIP page and extract seat count. None on
    failure (network, missing, parse error) — caller treats unknown as
    soft-pass."""
    if not vip_url:
        return None
    url = vip_url if vip_url.startswith("http") else _VIP_BASE + vip_url
    req = urllib.request.Request(url, headers={
        **HEADERS,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    m = _VIP_SEATS_RE.search(html)
    if not m:
        return None
    val = m.group(1) or m.group(2)
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def enrich_listings_with_seats(
    listings: List[dict],
    *,
    max_fetches: int = 1000,
    sleep_s: float = 0.25,
) -> List[dict]:
    """Mutate ``listings`` in place — fetch the VIP page for every entry
    whose ``model_key`` is set (whitelist-candidate) and add ``seats``.

    The search API caps at ~30 attributes per listing and doesn't include
    seat count, so we have to fetch the per-listing page. We only enrich
    listings that have a whitelist token in the title (the rest get
    classifier-rejected downstream anyway and the seat count is wasted).

    Bounded by ``max_fetches`` so a single refresh can't blow the GH
    Actions timeout — uncached listings beyond the cap stay
    ``seats=None`` and pass the soft-gate; the next refresh picks them
    up if they're still active.
    """
    enriched_count = 0
    skipped_no_token = 0
    for listing in listings:
        if enriched_count >= max_fetches:
            break
        if listing.get("seats") is not None:
            continue
        if not listing.get("model_key"):
            skipped_no_token += 1
            continue
        listing["seats"] = _fetch_vip_seats(listing.get("url") or "")
        enriched_count += 1
        time.sleep(sleep_s)
    if enriched_count:
        print(f"  marktplaats VIP seats: enriched {enriched_count} listings "
              f"(skipped {skipped_no_token} non-whitelist)")
    return listings


def fetch_market_prices(query: str, pages: int = 3) -> List[dict]:
    results = []
    for i in range(pages):
        listings = _fetch_page(query, offset=i * 100)
        if not listings:
            break
        for item in listings:
            parsed = _parse_listing(item)
            if parsed:
                results.append(parsed)
        time.sleep(0.4)
    return results


class PriceIndex:
    """(model_key, year) → sorted list of EUR asking prices."""

    def __init__(self, listings: List[dict]):
        self._data: Dict[tuple, List[float]] = {}
        for item in listings:
            mk = item.get("model_key")
            yr = item.get("year")
            price = item.get("price_eur")
            if mk and yr and price:
                self._data.setdefault((mk, yr), []).append(price)

    def median(
        self,
        model_key: Optional[str],
        year: Optional[int],
    ) -> Optional[float]:
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


def build_price_index(queries: List[str], pages_per_query: int = 3) -> "PriceIndex":
    all_listings: List[dict] = []
    for q in queries:
        print(f"  marktplaats: {q} ...", end=" ", flush=True)
        listings = fetch_market_prices(q, pages=pages_per_query)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return PriceIndex(all_listings)
