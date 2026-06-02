"""Gaspedaal.nl retail price index.

Gaspedaal is a Dutch aggregator that pulls listings from multiple dealers
(NL + BE). Search result pages embed a schema.org JSON-LD ItemList so no
Playwright is needed — plain HTTP + BeautifulSoup works.

URL pattern: https://www.gaspedaal.nl/{make}/{model}?page={n}
"""

import json
import time
import urllib.request
from typing import List, Optional

from bs4 import BeautifulSoup

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 55_000

_BASE_URL = "https://www.gaspedaal.nl"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# model_key → (make_slug, model_slug) for URL construction
_MODEL_SLUGS = {
    # whitelist (camper-candidate)
    "transit_custom":  ("ford",          "transit-custom"),
    "tourneo_custom":  ("ford",          "tourneo-custom"),
    "expert":          ("peugeot",       "expert"),
    "jumpy":           ("citroen",       "jumpy"),
    "proace":          ("toyota",        "proace"),
    "scudo":           ("fiat",          "scudo"),
    "vivaro":          ("opel",          "vivaro"),
    "trafic":          ("renault",       "trafic"),
    "primastar":       ("nissan",        "primastar"),
    "talento":         ("fiat",          "talento"),
    "vito":            ("mercedes-benz", "vito"),
    "vclass":          ("mercedes-benz", "v-klasse"),
    "staria":          ("hyundai",       "staria"),
    # whitelist (PSA L1H1/L2H1 group)
    "boxer":           ("peugeot",       "boxer"),
    "ducato":          ("fiat",          "ducato"),
    "jumper":          ("citroen",       "jumper"),
    "transporter":     ("volkswagen",    "transporter"),
    # legacy big-van — retained for the auction-side price-index fallback
    "transit":         ("ford",          "transit"),
    "sprinter":        ("mercedes-benz", "sprinter"),
    "master":          ("renault",       "master"),
    "crafter":         ("volkswagen",    "crafter"),
    "movano":          ("opel",          "movano"),
    "tge":             ("man",           "tge"),
    "daily":           ("iveco",         "daily"),
}


def _search_url(make: str, model: str, page: int = 1) -> str:
    url = f"{_BASE_URL}/{make}/{model}"
    if page > 1:
        url += f"?page={page}"
    return url


def _fetch_page(url: str) -> Optional[str]:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  gaspedaal fetch error ({url}): {e}")
        return None


def _extract_items(html: str) -> List[dict]:
    """Find the schema.org ItemList in the page and return its itemListElement items."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, ValueError):
            continue
        # Could be a single object or a list of objects
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if obj.get("@type") == "ItemList":
                elements = obj.get("itemListElement") or []
                return [el.get("item") or el for el in elements if isinstance(el, dict)]
    return []


def _parse_item(item: dict, model_key: str) -> Optional[dict]:
    """Extract price / year / km from one schema.org Car/Product item."""
    offers = item.get("offers") or {}
    try:
        price = float(offers.get("price") or 0)
    except (ValueError, TypeError):
        return None
    if not (PRICE_MIN_EUR <= price <= PRICE_MAX_EUR):
        return None

    try:
        year = int(item.get("productionDate") or 0) or None
    except (ValueError, TypeError):
        year = None

    km = None
    odometer = item.get("mileageFromOdometer") or {}
    try:
        km = int(odometer.get("value") or 0) or None
    except (ValueError, TypeError):
        km = None

    name = item.get("name") or ""
    brand = (item.get("brand") or {}).get("name") if isinstance(item.get("brand"), dict) else (item.get("brand") or "")
    model = item.get("model") or ""
    title = name or f"{brand} {model}".strip() or model_key

    return {
        "price_eur": price,
        "year": year,
        "km": km,
        "title": str(title),
        "model_key": model_key,
        "source": "gaspedaal",
    }


def fetch_market_prices(model_key: str, pages: int = 5) -> List[dict]:
    """Return parsed listings for one model token (e.g. "boxer")."""
    slugs = _MODEL_SLUGS.get(model_key)
    if not slugs:
        return []
    make, model = slugs
    results: List[dict] = []
    for page in range(1, pages + 1):
        url = _search_url(make, model, page)
        html = _fetch_page(url)
        if html is None:
            break
        items = _extract_items(html)
        if not items:
            break
        for item in items:
            parsed = _parse_item(item, model_key)
            if parsed:
                results.append(parsed)
        if len(items) < 30:
            break  # last page
        if page < pages:
            time.sleep(0.5)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 5,
) -> List[dict]:
    """Fetch Gaspedaal listings for all (or specified) model keys.

    Returns a flat list of listing dicts compatible with ``market_price.PriceIndex``.
    """
    keys = model_keys or list(_MODEL_SLUGS.keys())
    all_listings: List[dict] = []
    for key in keys:
        print(f"  gaspedaal: {key} ...", end=" ", flush=True)
        listings = fetch_market_prices(key, pages=pages_per_model)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return all_listings
