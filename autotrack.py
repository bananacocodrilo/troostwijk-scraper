"""AutoTrack.nl retail price index.

AutoTrack is a Dutch dealer-aggregator owned by the AutoScout24 Group
(separate from autoscout24.nl, with its own dealer pool). The search
page is a Next.js SPA — listing JSON is streamed inside RSC chunks
embedded as ``self.__next_f.push([1, "..."])`` script tags, no XHR
needed. We extract, brace-walk to each hit object, and JSON-parse.

Schema (relevant fields per hit):
    {
      "advertentieId":  59406388,
      "url":            "https://www.autotrack.nl/a/peugeot-boxer-diesel-2019-59406388",
      "autogegevens": {
        "algemeen":     {"merknaam":"Peugeot","modelnaam":"Boxer","uitvoering":"…"},
        "geschiedenis": {"kilometerstand":93665, "bouwjaar":2019},
      },
      "prijs": {"totaal":19500, "totaalInclusiefBtw":19500},
    }

URL shape (the search page silently 308-redirects ``?merk=&model=`` to
this canonical form, so we use the canonical form directly):
    /aanbod?data.merkModel.filter.0.slug={make}
           &data.merkModel.filter.0.models.0.slug={model}
           &pageSize=60&pageNumber={n}
"""

import json
import re
import time
import urllib.request
from typing import Dict, List, Optional

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 55_000
PAGE_SIZE     = 60   # autotrack caps at 60 per page

_BASE_URL = "https://www.autotrack.nl/aanbod"

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# model_key → (make_slug, model_slug) — both lowercased for the canonical URL.
# Whitelist canonicals + the legacy big-van set (cheap to fetch, useful for
# auction-side PriceIndex padding).
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
    # legacy big-van — auction-side fallback medians
    "transit":         ("ford",          "transit"),
    "sprinter":        ("mercedes-benz", "sprinter"),
    "master":          ("renault",       "master"),
    "crafter":         ("volkswagen",    "crafter"),
    "movano":          ("opel",          "movano"),
    "tge":             ("man",           "tge"),
    "daily":           ("iveco",         "daily"),
}


def _search_url(make: str, model: str, page: int = 1) -> str:
    return (
        f"{_BASE_URL}"
        f"?data.merkModel.filter.0.slug={make}"
        f"&data.merkModel.filter.0.models.0.slug={model}"
        f"&pageSize={PAGE_SIZE}&pageNumber={page}"
    )


def _fetch_page(url: str) -> Optional[str]:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  autotrack fetch error ({url[:80]}…): {e}")
        return None


def _extract_rsc_payload(html: str) -> str:
    """Concatenate every ``self.__next_f.push([1, "..."])`` chunk into one
    decoded string. The chunks are streamed RSC payload fragments — only
    when joined and unicode-unescaped do listing JSON objects line up."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.+?)"\]\)', html, re.DOTALL)
    if not chunks:
        return ""
    joined = "".join(chunks)
    return joined.encode("utf-8", errors="replace").decode("unicode_escape", errors="replace")


def _iter_hit_objects(payload: str):
    """Yield each ``{"beschikbaarheidsStatus":...}`` JSON object found in
    the RSC payload. Uses brace-depth walking to find object boundaries
    (the payload is too irregular to parse end-to-end). Caller decodes
    each yielded slice with ``json.loads``."""
    start_token = '{"beschikbaarheidsStatus"'
    i = 0
    while True:
        j = payload.find(start_token, i)
        if j < 0:
            return
        depth = 0
        end = j
        in_str = False
        esc = False
        for k in range(j, min(len(payload), j + 12000)):
            c = payload[k]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        yield payload[j:end]
        i = end


def _parse_hit(raw: str, fallback_model_key: str) -> Optional[dict]:
    try:
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None

    prijs = obj.get("prijs") or {}
    price = prijs.get("totaal") or prijs.get("totaalInclusiefBtw") or prijs.get("bedrag")
    if not isinstance(price, (int, float)):
        return None
    price = float(price)
    if not (PRICE_MIN_EUR <= price <= PRICE_MAX_EUR):
        return None

    auto = obj.get("autogegevens") or {}
    alg  = auto.get("algemeen") or {}
    gesc = auto.get("geschiedenis") or {}

    year = gesc.get("bouwjaar")
    if not isinstance(year, int):
        year = None
    km = gesc.get("kilometerstand")
    if not isinstance(km, int):
        km = None

    merk    = alg.get("merknaam") or ""
    model   = alg.get("modelnaam") or ""
    variant = alg.get("commercieleUitvoering") or alg.get("uitvoering") or ""
    title = " ".join(p for p in (merk, model, variant) if p).strip()
    if not title:
        return None

    url = obj.get("url") or ""
    if not url:
        return None

    return {
        "price_eur": price,
        "year":      year,
        "km":        km,
        "title":     title,
        "url":       url,
        "model_key": fallback_model_key,
        "source":    "autotrack",
        "body_type": alg.get("carrosserievormSlug"),
    }


def fetch_market_prices(model_key: str, pages: int = 4) -> List[dict]:
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
        payload = _extract_rsc_payload(html)
        if not payload:
            break
        page_n = 0
        for raw in _iter_hit_objects(payload):
            parsed = _parse_hit(raw, model_key)
            if parsed:
                results.append(parsed)
                page_n += 1
        if page_n == 0:
            break
        if page < pages:
            time.sleep(0.5)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 4,
) -> List[dict]:
    """Fetch AutoTrack listings for all (or specified) model keys.

    Returns a flat list of listing dicts compatible with ``market_price.PriceIndex``.
    """
    keys = model_keys or list(_MODEL_SLUGS.keys())
    all_listings: List[dict] = []
    for key in keys:
        print(f"  autotrack: {key} ...", end=" ", flush=True)
        listings = fetch_market_prices(key, pages=pages_per_model)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return all_listings
