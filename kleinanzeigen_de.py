"""Kleinanzeigen.de (DE) asking-price index.

German classifieds (formerly eBay Kleinanzeigen). NOT the Adevinta
``/lrp/api/search`` backend that Marktplaats / 2dehands share — this is a
separate platform that server-renders its search results as HTML. Plain
HTTP + BeautifulSoup works (no Playwright needed): each result is an
``<article class="aditem">`` with ``data-adid`` / ``data-href``, a price,
and KM / EZ (Erstzulassung = first-registration MM/YYYY) tags.

Listing dict matches the project convention:
    price_eur, year, km, title, url, source, images, model_key, country
"""

import re
import time
import urllib.parse
import urllib.request
from typing import List, Optional

from bs4 import BeautifulSoup

BASE = "https://www.kleinanzeigen.de"
# Two disjoint catalogues hold our vehicles:
#   Autos                    -> /s-autos/<keyword>/k0c216           (passenger / pkw)
#   Nutzfahrzeuge & Anhaenger -> /s-nutzfahrzeuge-anhaenger/<kw>/k0c280  (cargo vans)
# High-roof panel vans (Sprinter/Crafter/Transit/Ducato...) live almost
# entirely in the Nutzfahrzeuge category, which has ZERO overlap with Autos,
# so both must be queried. Paginated form: /<path>/seite:N/<keyword>/<cat>.
_CATEGORIES = [
    ("s-autos", "k0c216"),
    ("s-nutzfahrzeuge-anhaenger", "k0c280"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-DE,de;q=0.9",
}

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 60_000   # DE big-van high-roof stock runs higher than NL small vans

# model_key (canonical, matches autoscout24._MODEL_SLUGS keys) → DE search keyword.
# High-roof panel-van families first (the L2H2 focus), then the rest of the
# whitelist. The keyword is a free-text search within the Autos category.
_MODEL_SLUGS = {
    # high-roof panel vans
    "sprinter":       "mercedes-sprinter",
    "crafter":        "vw-crafter",
    "tge":            "man-tge",
    "transit":        "ford-transit",
    "ducato":         "fiat-ducato",
    "boxer":          "peugeot-boxer",
    "jumper":         "citroen-jumper",
    "master":         "renault-master",
    "movano":         "opel-movano",
    # small / mid whitelist
    "transit_custom": "ford-transit-custom",
    "vito":           "mercedes-vito",
    "vclass":         "mercedes-v-klasse",
    "expert":         "peugeot-expert",
    "jumpy":          "citroen-jumpy",
    "proace":         "toyota-proace",
    "vivaro":         "opel-vivaro",
    "trafic":         "renault-trafic",
    "transporter":    "vw-transporter",
    "staria":         "hyundai-staria",
    # Hochdach sweeps: model+keyword catches H2/H3 listings buried in generic
    # model searches. model_key is inferred from title by _infer_model_key.
    "_sprinter_hd":   "mercedes-sprinter hochdach",
    "_crafter_hd":    "vw-crafter hochdach",
    "_transit_hd":    "ford-transit hochdach",
    "_ducato_hd":     "fiat-ducato hochdach",
    "_jumper_hd":     "citroen-jumper hochdach",
    "_boxer_hd":      "peugeot-boxer hochdach",
    "_master_hd":     "renault-master hochdach",
    "_movano_hd":     "opel-movano hochdach",
}

# For the "_hochdach" sweep the model_key is inferred from the listing title
# (same tokens used by marktplaats._model_key). Sets contribute to the asking
# feed classification but not to PriceIndex medians (model_key stays None when
# no token matches — fine, medians come from the model-specific passes).
_TITLE_TOKENS = [
    ("transit custom", "transit_custom"), ("transit", "transit"),
    ("sprinter", "sprinter"), ("crafter", "crafter"), ("tge", "tge"),
    ("ducato", "ducato"), ("boxer", "boxer"), ("jumper", "jumper"),
    ("master", "master"), ("movano", "movano"),
    ("vito", "vito"), ("expert", "expert"), ("jumpy", "jumpy"),
    ("proace", "proace"), ("vivaro", "vivaro"), ("trafic", "trafic"),
    ("transporter", "transporter"),
]

def _infer_model_key(title: str) -> Optional[str]:
    s = title.lower()
    for token, key in _TITLE_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", s):
            return key
    return None

_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+)\s*€")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _search_url(path: str, cat: str, keyword: str, page: int = 1) -> str:
    if page <= 1:
        return f"{BASE}/{path}/{keyword}/{cat}"
    return f"{BASE}/{path}/seite:{page}/{keyword}/{cat}"


def _fetch_html(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  kleinanzeigen fetch error ({url}): {e}")
        return None


def _parse_price(text: str) -> Optional[float]:
    """First euro amount in a '7.500 € VB' style string. None if absent."""
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", ""))
    except ValueError:
        return None


def _parse_article(art) -> Optional[dict]:
    href = art.get("data-href") or ""
    title_el = art.select_one("h2 a, .text-module-begin a, a.ellipsis")
    if title_el is not None:
        title = title_el.get_text(strip=True)
        href = href or title_el.get("href") or ""
    else:
        title = ""
    if not title or not href:
        return None

    price_el = art.select_one(
        ".aditem-main--middle--price-shipping--price, .aditem-main--middle--price"
    )
    price_eur = _parse_price(price_el.get_text(strip=True)) if price_el else None
    if price_eur is None or not (PRICE_MIN_EUR <= price_eur <= PRICE_MAX_EUR):
        return None

    # Tags carry KM ("588.250 km") and EZ ("EZ 06/2018").
    year: Optional[int] = None
    km: Optional[int] = None
    for tag in art.select(".text-module-end .simpletag, .aditem-main--bottom .simpletag"):
        t = tag.get_text(strip=True)
        low = t.lower()
        if low.endswith("km"):
            digits = re.sub(r"[^\d]", "", t)
            km = int(digits) if digits else km
        elif "ez" in low or "/" in t:
            ym = _YEAR_RE.search(t)
            if ym:
                year = int(ym.group(0))

    images: List[str] = []
    img = art.select_one("img")
    if img is not None:
        src = img.get("src") or img.get("data-imgsrc") or img.get("data-src")
        if isinstance(src, str) and src.startswith("http"):
            images.append(src)

    return {
        "price_eur": price_eur,
        "year": year,
        "km": km,
        "title": title,
        "url": href if href.startswith("http") else BASE + href,
        "model_key": None,   # set by caller (the searched key)
        "source": "kleinanzeigen_de",
        "country": "de",
        "images": images,
    }


def fetch_market_prices(model_key: str, pages: int = 8) -> List[dict]:
    keyword = _MODEL_SLUGS.get(model_key)
    if not keyword:
        return []
    results: List[dict] = []
    seen: set = set()   # dedupe by ad id / url across both categories
    for path, cat in _CATEGORIES:
        for page in range(1, pages + 1):
            html = _fetch_html(_search_url(path, cat, keyword, page))
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            articles = soup.select("article.aditem")
            if not articles:
                break
            for art in articles:
                adid = art.get("data-adid") or ""
                parsed = _parse_article(art)
                if not parsed:
                    continue
                key = adid or parsed["url"]
                if key in seen:
                    continue
                seen.add(key)
                parsed["model_key"] = (
                    _infer_model_key(parsed.get("title", ""))
                    if model_key.startswith("_")
                    else model_key
                )
                results.append(parsed)
            time.sleep(0.5)
    return results


def build_listings(
    model_keys: Optional[List[str]] = None,
    pages_per_model: int = 8,
) -> List[dict]:
    target = model_keys or list(_MODEL_SLUGS.keys())
    all_listings: List[dict] = []
    for mk in target:
        print(f"  kleinanzeigen: {mk} ...", end=" ", flush=True)
        listings = fetch_market_prices(mk, pages=pages_per_model)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return all_listings


if __name__ == "__main__":
    out = build_listings(["sprinter"], pages_per_model=1)
    print(f"\ntotal: {len(out)}")
    for l in out[:5]:
        print(l)
