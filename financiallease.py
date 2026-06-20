"""Financiallease.nl (NL) asking-price index — upfront purchase price.

Financiallease.nl is a Magento storefront. We surface the **upfront
purchase price** (the ``td.item-price`` cell, e.g. "€ 7900") rather than
the monthly lease, so listings slot into the same cohort comparison as the
marketplace asking feeds.

Results come from the brand catalog pages ``/aanbod/<brand>?p=<page>``
(the ``/catalogsearch`` endpoint only exposes monthly prices). Each result
is a ``div.product-item-info`` card with a title link (``a.product-item-link``),
image, an upfront-price cell, and a spec table (km / kW / year / fuel). A
brand page mixes that brand's whole range; the model_key is resolved from
the title and non-whitelist models are dropped downstream by classify_vehicle.

Listing dict: price_eur, year, km, title, url, source, images, model_key, country
"""

import re
import time
import urllib.request
from typing import List, Optional

from bs4 import BeautifulSoup

BASE = "https://www.financiallease.nl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9",
}

PRICE_MIN_EUR = 1_500
PRICE_MAX_EUR = 60_000

# Brand catalog slugs that contain whitelist van families.
_BRANDS = [
    "mercedes-benz",   # sprinter / vito / v-klasse
    "volkswagen",      # crafter / transporter
    "ford",            # transit / transit custom
    "fiat",            # ducato / scudo / talento
    "peugeot",         # boxer / expert
    "citroen",         # jumper / jumpy
    "renault",         # master / trafic
    "opel",            # movano / vivaro
    "man",             # tge
    "toyota",          # proace
    "nissan",          # primastar / interstar
    "hyundai",         # staria
]

# title token → canonical model_key (matches autoscout24._MODEL_SLUGS keys).
# Multi-word tokens first so "transit custom" beats bare "transit".
_KEY_TOKENS = [
    ("transit custom", "transit_custom"),
    ("tourneo custom", "tourneo_custom"),
    ("v-klasse", "vclass"), ("v klasse", "vclass"), ("v-class", "vclass"),
    ("sprinter", "sprinter"), ("crafter", "crafter"), ("tge", "tge"),
    ("transit", "transit"), ("transporter", "transporter"),
    ("ducato", "ducato"), ("boxer", "boxer"), ("jumper", "jumper"),
    ("master", "master"), ("movano", "movano"), ("interstar", "interstar"),
    ("vito", "vito"), ("expert", "expert"), ("jumpy", "jumpy"),
    ("proace", "proace"), ("vivaro", "vivaro"), ("trafic", "trafic"),
    ("primastar", "primastar"), ("talento", "talento"), ("scudo", "scudo"),
    ("staria", "staria"),
]

_PRICE_RE = re.compile(r"€\s*([\d.]+)")
_KM_RE = re.compile(r"([\d.]+)\s*km", re.IGNORECASE)


def _model_key(title: str) -> Optional[str]:
    s = (title or "").lower()
    for tok, key in _KEY_TOKENS:
        if tok in s:
            return key
    return None


def _fetch_html(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=25) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  financiallease fetch error ({url}): {e}")
        return None


def _num(s: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def _parse_card(card) -> Optional[dict]:
    a = card.select_one("a.product-item-link")
    if a is None:
        return None
    title = a.get_text(strip=True)
    href = a.get("href") or ""
    if not title or not href:
        return None

    price_el = card.select_one("td.item-price")
    price_eur = None
    if price_el is not None:
        m = _PRICE_RE.search(price_el.get_text(strip=True))
        price_eur = _num(m.group(1)) if m else None
    if price_eur is None or not (PRICE_MIN_EUR <= price_eur <= PRICE_MAX_EUR):
        return None

    text = card.get_text(" ", strip=True)
    kmm = _KM_RE.search(text)
    km = _num(kmm.group(1)) if kmm else None
    year = None
    for td in card.select("div.product-item-specs td, table td"):
        t = td.get_text(strip=True)
        if re.fullmatch(r"(19|20)\d{2}", t):
            year = int(t)
            break

    images: List[str] = []
    img = card.find("img")
    if img is not None:
        src = img.get("src") or img.get("data-src")
        if isinstance(src, str) and src.startswith("http"):
            images.append(src)

    return {
        "price_eur": float(price_eur),
        "year": year,
        "km": km,
        "title": title,
        "url": href if href.startswith("http") else BASE + href,
        "model_key": _model_key(title),
        "source": "financiallease",
        "country": "nl",
        "images": images,
    }


def fetch_brand(brand: str, pages: int = 2) -> List[dict]:
    results: List[dict] = []
    for page in range(1, pages + 1):
        html = _fetch_html(f"{BASE}/aanbod/{brand}?p={page}")
        if not html:
            break
        cards = BeautifulSoup(html, "html.parser").select("div.product-item-info")
        if not cards:
            break
        before = len(results)
        for card in cards:
            parsed = _parse_card(card)
            if parsed:
                results.append(parsed)
        if len(results) == before:
            break
        time.sleep(0.5)
    return results


def build_listings(
    brands: Optional[List[str]] = None,
    pages_per_brand: int = 2,
) -> List[dict]:
    target = brands or _BRANDS
    all_listings: List[dict] = []
    for brand in target:
        print(f"  financiallease: {brand} ...", end=" ", flush=True)
        listings = fetch_brand(brand, pages=pages_per_brand)
        print(f"{len(listings)} listings")
        all_listings.extend(listings)
    return all_listings


if __name__ == "__main__":
    out = build_listings(["mercedes-benz", "fiat"], pages_per_brand=1)
    print(f"\ntotal: {len(out)}")
    for l in out[:6]:
        print(l)
