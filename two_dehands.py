"""2dehands.be retail price index.

2dehands.be is the Belgian equivalent of Marktplaats.nl (same Adevinta group).
The API structure is identical — same /lrp/api/search endpoint, same response
shape (listings[n].priceInfo.priceCents, listings[n].attributes with
constructionYear and mileage keys).
"""

import json
import time
import urllib.parse
import urllib.request
from typing import List, Optional

SEARCH_URL = "https://www.2dehands.be/lrp/api/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "nl-BE,nl;q=0.9",
    "Referer": "https://www.2dehands.be/",
}

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 45_000

# 2dehands.be URL prefixes for non-vehicle categories (parts,
# accessories, misc). Same `/v/` shape as Marktplaats — Adevinta API
# powers both. Drop these at parse time so they never enter the cache
# or the feed.
_EXCLUDED_CATEGORY_PREFIXES = (
    "/v/auto-onderdelen/",
    "/v/auto-diversen/",
    "/v/caravans-mobilhomes-en-kamperen/camper-accessoires/",
    "/v/caravans-mobilhomes-en-kamperen/onderdelen-en-accessoires/",
)

# Default queries used by build_listings() when none are supplied
DEFAULT_QUERIES = [
    # whitelist (camper-candidate)
    "Ford Transit Custom", "Ford Tourneo Custom",
    "Peugeot Expert", "Citroen Jumpy", "Toyota ProAce",
    "Fiat Scudo",
    "Opel Vivaro", "Renault Trafic", "Nissan Primastar", "Fiat Talento",
    "Volkswagen Transporter",
    "Peugeot Boxer", "Citroen Jumper", "Fiat Ducato",
    "Mercedes Vito", "Mercedes V-Klasse",
    "Hyundai Staria",
    # legacy big-van
    "Mercedes Sprinter", "Ford Transit", "Renault Master",
    "Volkswagen Crafter", "Opel Movano", "MAN TGE", "Iveco Daily",
]

# Token → canonical model_key used by PriceIndex bucketing.
# Multi-word tokens MUST precede single-word substrings (e.g.
# "transit custom" before "transit") because _model_key() iterates this
# dict in insertion order.
_MODEL_KEYS = {
    # whitelist multi-word
    "transit custom":  "transit_custom",
    "tourneo custom":  "tourneo_custom",
    "v-klasse":        "vclass",
    "v klasse":        "vclass",
    "v-class":         "vclass",
    # whitelist single-word
    "boxer":     "boxer",
    "jumper":    "jumper",
    "ducato":    "ducato",
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
    # legacy big-van
    "sprinter":  "sprinter",
    "master":    "master",
    "crafter":   "crafter",
    "movano":    "movano",
    "tge":       "tge",
    "daily":     "daily",
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
        "numberOfResultsPerPage": 100,
        "offset": offset,
    })
    req = urllib.request.Request(f"{SEARCH_URL}?{params}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("listings", [])
    except Exception as e:
        print(f"  2dehands fetch error ({query} offset={offset}): {e}")
        return []


def _parse_listing(item: dict) -> Optional[dict]:
    price_info = item.get("priceInfo") or {}
    cents = price_info.get("priceCents")
    if not isinstance(cents, (int, float)):
        return None
    price_eur = cents / 100
    if not (PRICE_MIN_EUR <= price_eur <= PRICE_MAX_EUR):
        return None

    # Drop non-vehicle categories (parts, accessories, misc) — see _EXCLUDED_CATEGORY_PREFIXES.
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
        "source": "2dehands",
        "images": images,
    }


def fetch_market_prices(query: str, pages: int = 3) -> List[dict]:
    """Return parsed listings for one search query."""
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


def build_listings(
    queries: Optional[List[str]] = None,
    pages_per_query: int = 3,
) -> List[dict]:
    """Fetch 2dehands.be listings for all (or specified) search queries.

    Returns a flat list of listing dicts compatible with ``market_price.PriceIndex``.
    """
    qs = queries or DEFAULT_QUERIES
    all_listings: List[dict] = []
    for q in qs:
        print(f"  2dehands: {q} ...", end=" ", flush=True)
        listings = fetch_market_prices(q, pages=pages_per_query)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return all_listings
