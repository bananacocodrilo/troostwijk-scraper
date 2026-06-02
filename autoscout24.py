"""AutoScout24 retail price index — multi-country.

Scrapes asking prices for large cargo vans from AutoScout24 across NL,
DE, FR, and BE. Used alongside Marktplaats/mobile.de/lacentrale in
``market_price.py`` to build a cross-border market reference.

Pages are Next.js SSR — vehicle data lives in ``__NEXT_DATA__`` JSON,
so no Playwright needed — plain HTTP with BeautifulSoup works.
The JSON structure is identical across all country TLDs.
"""

import json
import time
import urllib.request
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

# Supported country TLDs → (base_url, Accept-Language header)
COUNTRIES: Dict[str, tuple] = {
    "nl": ("https://www.autoscout24.nl/lst", "nl-NL,nl;q=0.9"),
    "de": ("https://www.autoscout24.de/lst", "de-DE,de;q=0.9"),
    # .fr and .be return 404 for all van models — different URL scheme, not worth maintaining
}

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Listings outside this range are likely parts, camper conversions, or errors.
PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 55_000

# make-slug, model-slug for AutoScout24 URL construction
_MODEL_SLUGS: Dict[str, tuple] = {
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


def _search_url(make: str, model: str, page: int = 1, country: str = "nl") -> str:
    base, _ = COUNTRIES.get(country, COUNTRIES["nl"])
    return (
        f"{base}/{make}/{model}"
        f"?sort=standard&desc=0&offer=U&ustate=N%2CU"
        f"&size=20&page={page}&atype=C"
    )


def _fetch_page(url: str, country: str = "nl") -> Optional[dict]:
    """Fetch one search page and return the parsed __NEXT_DATA__ dict."""
    _, lang = COUNTRIES.get(country, COUNTRIES["nl"])
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": lang,
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  autoscout24 fetch error ({url}): {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_listings(data: dict) -> List[dict]:
    """Navigate __NEXT_DATA__ to the listings array."""
    try:
        page_props = data["props"]["pageProps"]
    except (KeyError, TypeError):
        return []

    # AutoScout24 nests listings under several possible keys depending on version.
    for key in ("listings", "initialState"):
        if key in page_props:
            blob = page_props[key]
            # listings is either a list directly or {"items": [...]}
            if isinstance(blob, list):
                return blob
            if isinstance(blob, dict):
                for sub in ("items", "data", "results"):
                    if isinstance(blob.get(sub), list):
                        return blob[sub]

    # Fallback: look one level deeper in any dict value
    for val in page_props.values():
        if isinstance(val, dict):
            for sub in ("items", "listings", "data"):
                if isinstance(val.get(sub), list):
                    return val[sub]

    return []


def _parse_price(s: str) -> Optional[float]:
    """Parse AutoScout24 formatted price string to EUR float.
    Handles "€ 14.995", "€14,995", "14.995" etc.
    Dutch locale uses period as thousands separator."""
    if not isinstance(s, str):
        return None
    # Strip currency symbol and whitespace
    s = s.replace("€", "").replace(",", "").replace(".", "").strip()
    # Remove trailing dash (e.g. "€ 19.354,-")
    s = s.rstrip("-").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_listing(item: dict, model_key: str) -> Optional[dict]:
    """Extract price / year / km from one listing object.

    AutoScout24 NL (2025) structure:
      item.price.priceFormatted   — "€ 14.995"
      item.vehicle.mileageInKm    — "121.382 km"
      item.tracking.firstRegistration — "12-2020"
      item.url                    — relative path
    """
    # --- price ---
    price = None
    price_str = (item.get("price") or {}).get("priceFormatted") or ""
    if price_str:
        price = _parse_price(price_str)
    if price is None:
        return None
    if not (PRICE_MIN_EUR <= price <= PRICE_MAX_EUR):
        return None

    # --- year --- (from "MM-YYYY" tracking string)
    year = None
    first_reg = (item.get("tracking") or {}).get("firstRegistration") or ""
    if first_reg and "-" in first_reg:
        try:
            yr = int(first_reg.split("-")[-1])
            if 1990 < yr < 2030:
                year = yr
        except (ValueError, IndexError):
            pass

    # --- km ---
    km = None
    km_str = (item.get("vehicle") or {}).get("mileageInKm") or ""
    if km_str:
        digits = "".join(c for c in km_str if c.isdigit())
        if digits:
            try:
                km = int(digits)
            except ValueError:
                pass

    # --- title ---
    vehicle = item.get("vehicle") or {}
    title = (
        item.get("title")
        or vehicle.get("modelVersionInput")
        or f"{vehicle.get('make', '')} {vehicle.get('model', '')}".strip()
        or model_key
    )

    # relative URL → leave as-is; caller can prefix https://www.autoscout24.nl
    url = item.get("url") or ""

    return {
        "price_eur": price,
        "year": year,
        "km": km,
        "title": str(title),
        "url": url,
        "model_key": model_key,
        "source": "autoscout24",
    }


def fetch_market_prices(
    model_key: str,
    pages: int = 4,
    country: str = "nl",
) -> List[dict]:
    """Return parsed listings for one model token (e.g. "boxer") and country."""
    slugs = _MODEL_SLUGS.get(model_key)
    if not slugs:
        return []
    make, model = slugs
    results: List[dict] = []
    for page in range(1, pages + 1):
        url = _search_url(make, model, page, country=country)
        data = _fetch_page(url, country=country)
        if data is None:
            break
        items = _extract_listings(data)
        if not items:
            break
        for item in items:
            parsed = _parse_listing(item, model_key)
            if parsed:
                parsed["source"] = f"autoscout24_{country}"
                results.append(parsed)
        if len(items) < 20:
            break  # last page
        time.sleep(0.6)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 4,
    countries: Optional[List[str]] = None,
) -> List[dict]:
    """Fetch AutoScout24 listings for all (or specified) model keys and countries.

    Returns a flat list of listing dicts compatible with ``market_price.PriceIndex``.
    Defaults to all supported countries."""
    keys = model_keys or list(_MODEL_SLUGS.keys())
    target_countries = countries or list(COUNTRIES.keys())
    all_listings: List[dict] = []
    for country in target_countries:
        for key in keys:
            print(f"  autoscout24.{country}: {key} ...", end=" ", flush=True)
            listings = fetch_market_prices(key, pages=pages_per_model, country=country)
            print(f"{len(listings)} listings")
            all_listings.extend(listings)
    return all_listings
