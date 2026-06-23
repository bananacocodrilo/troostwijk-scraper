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

import vat

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
    ext   = {a["key"]: a.get("value") for a in (item.get("extendedAttributes") or [])}
    # Defensive structured VAT hint: Adevinta sometimes exposes a BTW/VAT
    # attribute on car listings. Map its value to a scheme; None if absent.
    vat_hint = None
    for _k, _val in {**attrs, **ext}.items():
        if any(t in str(_k).lower() for t in ("btw", "vat", "mwst")):
            vat_hint = vat.hint_from_text(_val)
            if vat_hint:
                break
    try:
        year = int(attrs.get("constructionYear") or 0) or None
    except (ValueError, TypeError):
        year = None
    try:
        km_raw = str(attrs.get("mileage") or "").replace(".", "").replace(",", "")
        km = int(km_raw) if km_raw.isdigit() else None
    except (ValueError, TypeError):
        km = None

    # Seat count: extendedAttributes "numberOfSeats" value is e.g. "6 stoelen".
    # Parse the leading digit. Falls back to None (VIP enrichment fills later).
    seats: Optional[int] = None
    seat_raw = ext.get("numberOfSeats") or ""
    if seat_raw:
        import re as _re
        m = _re.match(r"(\d+)", str(seat_raw).strip())
        if m:
            seats = int(m.group(1))

    title = item.get("title") or ""
    # Description: seller-written free text, often contains seat count,
    # crew-cab markers ("6 stoelen", "kombi", "dubbele cabine"), or cargo
    # signals ("bestelwagen", "laadbak") that the title omits. Stored as
    # `description` so asking_feed can pass it as `remarks` to strict_filter
    # and the scoring functions.
    description = (item.get("description") or "").strip()

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
        "price_eur":   price_eur,
        "year":        year,
        "km":          km,
        "title":       title,
        "description": description,
        "url":         item.get("vipUrl") or "",
        "model_key":   _model_key(title),
        "images":      images,
        "seats":       seats,  # None unless extendedAttributes or VIP fill it
        "vat_hint":    vat_hint,
    }


# Marktplaats VIP (per-listing detail) page embeds the seat count. The
# format changed in 2026: the old structured JSON attribute
#   {"key":"numberOfSeats","label":"Aantal stoelen","value":"3",...}
# is gone — current pages render a spec row instead:
#   <dt>Aantal zitplaatsen</dt><dd>6</dd>   (value sits in a sibling tag)
# Primary pattern reads that rendered row; the legacy JSON-attribute and
# JSON-LD forms are kept as fallbacks for any page still serving them.
# Listings without the field (seller left it blank) yield None → unknown →
# soft-pass, same as before.
_VIP_SEATS_RE = re.compile(
    r'Aantal (?:zitplaatsen|stoelen)\s*:?\s*(?:<[^>]+>\s*){0,3}([0-9]{1,2})\b'
    r'|"key"\s*:\s*"numberOfSeats"[^}]{0,120}?"value"\s*:\s*"?(\d+)'
    r'|"vehicleSeatingCapacity"\s*:\s*"?(\d+)',
    re.IGNORECASE,
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
    val = m.group(1) or m.group(2) or m.group(3)
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def enrich_listings_with_seats(
    listings: List[dict],
    *,
    max_fetches: int = 200,
    sleep_s: float = 0.25,
    workers: int = 3,
) -> List[dict]:
    """Mutate ``listings`` in place — fetch the VIP page for every entry
    whose ``model_key`` is set (whitelist-candidate) and add ``seats``.

    Fetches run in a small thread pool (default 3 workers) so wall-clock
    time is ~workers× faster than serial. Each thread sleeps ``sleep_s``
    after its request to avoid hammering Marktplaats.
    """
    import concurrent.futures

    candidates = [
        l for l in listings
        if l.get("seats") is None and l.get("model_key")
    ][:max_fetches]

    if not candidates:
        return listings

    def _fetch_one(listing):
        url = listing.get("url") or ""
        seats = _fetch_vip_seats(url)
        time.sleep(sleep_s)
        return listing, seats

    enriched_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for listing, seats in pool.map(_fetch_one, candidates):
            if seats is not None:
                listing["seats"] = seats
                enriched_count += 1

    skipped = sum(1 for l in listings if not l.get("model_key") and l.get("seats") is None)
    if enriched_count or candidates:
        print(f"  marktplaats VIP seats: enriched {enriched_count}/{len(candidates)} listings "
              f"(skipped {skipped} non-whitelist, cap={max_fetches})")
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
