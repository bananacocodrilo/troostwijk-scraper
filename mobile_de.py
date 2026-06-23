"""mobile.de (DE) asking-price index — best-effort.

mobile.de sits behind Akamai Bot Manager, which hard-blocks datacenter /
CI IP ranges with a JS sensor challenge (HTTP 403, or a 200 "behavioral
content" interstitial). From a residential IP — or through a residential
proxy — the consumer JSON API is reachable; from GitHub Actions it is not.

This module therefore degrades cleanly: it tries the consumer API with a
TLS-impersonating client (curl_cffi), optionally through a proxy set in
the ``MOBILE_DE_PROXY`` env var, detects the Akamai challenge, and returns
``[]`` when blocked rather than raising. When it IS reachable it parses the
consumer SRP JSON into the project's standard listing dict.

Listing dict: price_eur, year, km, title, url, source, images, model_key, country
"""

import os
import re
import time
from typing import List, Optional

try:
    from curl_cffi import requests as _creq
except Exception:                       # curl_cffi optional at import time
    _creq = None

CONSUMER_SRP = "https://m.mobile.de/consumer/api/search/srp"
WEB_BASE = "https://suchen.mobile.de"

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 60_000

# Akamai challenge fingerprints — if any appears the response is not real data.
_BLOCK_MARKERS = ("Access denied", "Zugriff verweigert", "sec-if-cpt-container",
                  "Reference&#32;", "Powered and protected")

# model_key (canonical, matches autoscout24._MODEL_SLUGS keys) → mobile.de
# (makeId, modelId) for the consumer API `ms=<make>;<model>;;` param. The
# high-roof panel-van families are the priority for the L2H2 pivot.
#   make ids: Mercedes 17200, Ford 9000, VW 25200, Fiat 8800, Peugeot 19000,
#             Citroen 5300, Renault 20700, Opel 19100, MAN 16800
_MODEL_MS = {
    "sprinter":       "17200;38",
    "vito":           "17200;36",
    "vclass":         "17200;65",
    "crafter":        "25200;26",
    "transporter":    "25200;67",
    "transit":        "9000;42",
    "transit_custom": "9000;121",
    "ducato":         "8800;26",
    "boxer":          "19000;15",
    "jumper":         "5300;15",
    "master":         "20700;30",
    "movano":         "19100;33",
    "tge":            "16800;3",
    "expert":         "19000;19",
    "vivaro":         "19100;52",
    "trafic":         "20700;46",
}


def _proxies() -> Optional[dict]:
    p = os.environ.get("MOBILE_DE_PROXY")
    return {"http": p, "https": p} if p else None


def _looks_blocked(text: str) -> bool:
    return any(m in text for m in _BLOCK_MARKERS)


def _fetch_json(ms: str, page: int) -> Optional[dict]:
    """Return parsed consumer-API JSON, or None if unreachable/blocked."""
    if _creq is None:
        return None
    params = {"vc": "Car", "page": str(page), "ms": f"{ms};;"}
    try:
        r = _creq.get(
            CONSUMER_SRP, params=params, impersonate="chrome120", timeout=25,
            headers={"Accept": "application/json", "Accept-Language": "de-DE,de;q=0.9"},
            proxies=_proxies(),
        )
    except Exception as e:
        print(f"  mobile.de fetch error (ms={ms} p{page}): {e}")
        return None
    if r.status_code != 200 or _looks_blocked(r.text):
        return None
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _to_int(val) -> Optional[int]:
    if val is None:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None


def _parse_item(item: dict, model_key: str) -> Optional[dict]:
    # The consumer SRP item shape varies; pull defensively from the common keys.
    price = (
        (item.get("price") or {}).get("gross")
        or (item.get("price") or {}).get("amount")
        or item.get("consumerPriceGross")
    )
    price_eur = _to_int(price)
    if price_eur is None or not (PRICE_MIN_EUR <= price_eur <= PRICE_MAX_EUR):
        return None

    attrs = item.get("attributes") or item.get("attr") or {}
    fr = attrs.get("firstRegistration") or item.get("firstRegistration")
    year = None
    if fr:
        m = re.search(r"(19|20)\d{2}", str(fr))
        year = int(m.group(0)) if m else None
    km = _to_int(attrs.get("mileage") or item.get("mileage"))

    title = item.get("title") or item.get("makeModel") or ""
    ad_id = item.get("id") or item.get("adId") or ""
    url = item.get("relativeUrl") or item.get("url") or ""
    if url and not url.startswith("http"):
        url = WEB_BASE + ("" if url.startswith("/") else "/") + url
    elif not url and ad_id:
        url = f"{WEB_BASE}/fahrzeuge/details.html?id={ad_id}"

    images: List[str] = []
    for img in (item.get("images") or []):
        u = img.get("uri") or img.get("url") if isinstance(img, dict) else img
        if isinstance(u, str) and u.startswith("http"):
            images.append(u)
        if len(images) >= 5:
            break
    if not images:
        prev = item.get("previewImage") or {}
        u = prev.get("uri") if isinstance(prev, dict) else None
        if isinstance(u, str) and u.startswith("http"):
            images.append(u)

    if not title:
        return None
    return {
        "price_eur": float(price_eur),
        "year": year,
        "km": km,
        "title": title,
        "url": url,
        "model_key": model_key,
        "source": "mobile_de",
        "country": "de",
        "images": images,
        # We capture price.gross (consumer incl-VAT price) above, so the
        # displayed price already includes VAT → no grossing-up needed.
        "vat_hint": "incl",
    }


def _items_from_payload(payload: dict) -> list:
    for key in ("items", "ads", "results", "searchResults"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


def fetch_market_prices(model_key: str, pages: int = 3) -> List[dict]:
    ms = _MODEL_MS.get(model_key)
    if not ms:
        return []
    results: List[dict] = []
    for page in range(1, pages + 1):
        payload = _fetch_json(ms, page)
        if not payload:
            break                       # blocked or empty — stop early
        items = _items_from_payload(payload)
        if not items:
            break
        for it in items:
            parsed = _parse_item(it, model_key)
            if parsed:
                results.append(parsed)
        time.sleep(0.5)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 3,
) -> List[dict]:
    if _creq is None:
        print("  mobile.de: curl_cffi not installed — skipping")
        return []
    target = model_keys or list(_MODEL_MS.keys())
    all_listings: List[dict] = []
    for mk in target:
        listings = fetch_market_prices(mk, pages=pages_per_model)
        all_listings.extend(listings)
    if not all_listings:
        print("  mobile.de: 0 listings (Akamai-blocked from this IP — needs "
              "a residential MOBILE_DE_PROXY or a residential/local IP)")
    else:
        print(f"  mobile.de: {len(all_listings)} listings")
    return all_listings


if __name__ == "__main__":
    out = build_listings(["sprinter"], pages_per_model=1)
    print(f"total: {len(out)}")
    for l in out[:3]:
        print(l)
