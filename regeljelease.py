"""Regeljelease.nl (NL) asking-price index — upfront purchase price.

Regeljelease is a financial-lease aggregator (React + Sanity CMS) with a
huge inventory (~42k vehicles). We deliberately surface the **upfront
purchase price** (``price.value``, ex-VAT) — not the monthly lease — so
listings slot into the same cohort comparison as the marketplace asking
feeds.

The SPA renders per-model SEO pages at ``/aanbod/<brand>/<model>`` whose
HTML embeds the full vehicle objects as an HTML-escaped ``"vehicles":[...]``
JSON blob (the same shape the ``/api/aanbod`` endpoint returns, but the
endpoint ignores filter/pagination params for anonymous GETs, so we scrape
the filtered pages instead). Each page yields ~12 vehicles.

Listing dict: price_eur, year, km, title, url, source, images, model_key, country
"""

import html as _html
import json
import re
import time
import urllib.request
from typing import List, Optional

BASE = "https://www.regeljelease.nl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9",
}

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 60_000

# model_key (canonical) → /aanbod/<brand>/<model> SEO path slug.
_MODEL_SLUGS = {
    "sprinter":       "mercedes-benz/sprinter",
    "vito":           "mercedes-benz/vito",
    "vclass":         "mercedes-benz/v-klasse",
    "crafter":        "volkswagen/crafter",
    "transporter":    "volkswagen/transporter",
    "transit":        "ford/transit",
    "transit_custom": "ford/transit-custom",
    "ducato":         "fiat/ducato",
    "boxer":          "peugeot/boxer",
    "jumper":         "citroen/jumper",
    "master":         "renault/master",
    "movano":         "opel/movano",
    "tge":            "man/tge",
    "expert":         "peugeot/expert",
    "vivaro":         "opel/vivaro",
    "trafic":         "renault/trafic",
}


def _fetch_html(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  regeljelease fetch error ({url}): {e}")
        return None


def _extract_vehicles(page_html: str) -> list:
    """Pull the embedded ``"vehicles":[...]`` array out of the page HTML.

    The blob is HTML-entity-escaped, so unescape first, then balance-walk
    the brackets from the first ``"vehicles":[`` occurrence."""
    txt = _html.unescape(page_html)
    i = txt.find('"vehicles":[')
    if i == -1:
        return []
    start = txt.index("[", i)
    depth = 0
    for j in range(start, len(txt)):
        c = txt[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[start:j + 1])
                except json.JSONDecodeError:
                    return []
    return []


def _parse_vehicle(v: dict, model_key: str) -> Optional[dict]:
    price = (v.get("price") or {}).get("value")
    if not isinstance(price, (int, float)):
        price = (v.get("priceConsumer") or {}).get("value")
    if not isinstance(price, (int, float)) or not (PRICE_MIN_EUR <= price <= PRICE_MAX_EUR):
        return None

    title = " ".join(s for s in [v.get("brand"), v.get("model"), v.get("edition")] if s).strip()
    if not title:
        return None

    year = v.get("productionYear")
    if not year:
        fa = str(v.get("firstWorldwideAdmission") or "")
        m = re.search(r"(19|20)\d{2}", fa)
        year = int(m.group(0)) if m else None

    km = v.get("mileage") if isinstance(v.get("mileage"), int) else None

    url = v.get("url") or v.get("primaryUrl") or v.get("urlPath") or ""
    if url and not url.startswith("http"):
        url = BASE + ("" if url.startswith("/") else "/") + url

    images: List[str] = []
    for img in (v.get("images") or []):
        u = img.get("url") or img.get("smallUrl") if isinstance(img, dict) else None
        if isinstance(u, str) and u.startswith("http"):
            images.append(u)
        if len(images) >= 5:
            break

    return {
        "price_eur": float(price),
        "year": int(year) if year else None,
        "km": km,
        "title": title,
        "url": url,
        "model_key": model_key,
        "source": "regeljelease",
        "country": "nl",
        "images": images,
    }


def fetch_market_prices(model_key: str, pages: int = 1) -> List[dict]:
    slug = _MODEL_SLUGS.get(model_key)
    if not slug:
        return []
    # The page is not paginatable for anonymous GETs (params ignored), so
    # ``pages`` is accepted for API symmetry but only page 1 is fetched.
    page_html = _fetch_html(f"{BASE}/aanbod/{slug}")
    if not page_html:
        return []
    results: List[dict] = []
    for v in _extract_vehicles(page_html):
        parsed = _parse_vehicle(v, model_key)
        if parsed:
            results.append(parsed)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 1,
) -> List[dict]:
    target = model_keys or list(_MODEL_SLUGS.keys())
    all_listings: List[dict] = []
    for mk in target:
        print(f"  regeljelease: {mk} ...", end=" ", flush=True)
        listings = fetch_market_prices(mk, pages=pages_per_model)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
        time.sleep(0.4)
    return all_listings


if __name__ == "__main__":
    out = build_listings(["sprinter", "crafter"], pages_per_model=1)
    print(f"\ntotal: {len(out)}")
    for l in out[:5]:
        print(l)
