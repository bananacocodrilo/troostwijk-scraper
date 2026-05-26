"""Troostwijk lot scraper.

Pages are Next.js SSR — every spec field we need is in the ``__NEXT_DATA__``
JSON blob (``props.pageProps.lot``). We parse that directly instead of
chasing regex matches across rendered HTML.
"""

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from models import ScoreBreakdown, Vehicle
from van_intel import evaluate

BASE = "https://www.troostwijkauctions.com/en/search"
ORIGIN = "https://www.troostwijkauctions.com"

# Map from the ``name`` field on lot.attributes -> our Vehicle field.
# Names are stable strings provided by the Troostwijk backend.
ATTR_MAP = {
    "Brand": "brand",
    "Type": "model",
    "Construction date": "year",
    "Date of First Admission": "first_registration",
    "Mileage during intake (km)": "km",
    "Mileage": "km",
    "Fuel type": "fuel",
    "Transmission": "transmission",
    "Power(kW)": "power_kw",
    "Cylinder capacity": "cylinder_cc",
    "Seat count": "seats",
    "Door count": "doors",
    "Color": "color",
    "Empty weight": "weight_kg",
    "Load capacity": "load_kg",
    "Emission standard": "emission_standard",
    "VIN": "vin",
    "Chassis number": "vin",
}

INT_FIELDS = {"year", "km", "power_kw", "cylinder_cc", "seats", "doors", "weight_kg", "load_kg"}


def build_search_url(query, year_min=None, year_max=None, page=1):
    url = f"{BASE}?page={page}&pageSize=48&searchTerm={query}&sort=relevance"
    if year_min and year_max:
        url += f"&yearsBuilt={year_min}%2C{year_max}"
    return url


def _new_context(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    )
    return browser, context


def get_lot_urls(query, pages=1, year_min=None, year_max=None):
    urls = []
    seen = set()

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for i in range(1, pages + 1):
            url = build_search_url(query, year_min, year_max, i)
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
            except Exception as e:
                print(f"search page failed {url}: {e}")
                continue

            soup = BeautifulSoup(page.content(), "html.parser")
            for a in soup.select("a[href*='/l/']"):
                href = a.get("href") or ""
                if href.startswith("/"):
                    href = ORIGIN + href
                if not re.search(r"/l/[^/]+-A\d+-\d+", href):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                urls.append(href)

        browser.close()

    return urls


def _next_data(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, "", "null"):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    # strip thousand separators and unit suffixes
    s = re.sub(r"[^\d\-]", "", s)
    if not s or s == "-":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_first_registration(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Sometimes only a year is given.
    if re.fullmatch(r"(19|20)\d{2}", s):
        return date(int(s), 1, 1)
    return None


def _attrs_to_dict(lot: dict) -> dict:
    """Turn ``lot.attributes`` (list of {name, unit, value}) into a flat dict."""
    out: dict = {}
    for attr in lot.get("attributes") or []:
        name = attr.get("name")
        raw = attr.get("value")
        if name is None or raw in (None, ""):
            continue
        field = ATTR_MAP.get(name)
        if not field:
            continue
        if field in INT_FIELDS:
            n = _to_int(raw)
            if n is not None:
                out[field] = n
        elif field == "first_registration":
            d = _parse_first_registration(raw)
            if d is not None:
                out[field] = d
        else:
            out[field] = str(raw).strip()
    return out


def _normalize_fuel(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().lower()
    if "diesel" in s:
        return "diesel"
    if "petrol" in s or "benzine" in s or "gasoline" in s:
        return "petrol"
    if "electric" in s or "elektrisch" in s:
        return "electric"
    if "lpg" in s or "cng" in s:
        return "lpg"
    if "hybrid" in s:
        return "hybrid"
    return s


def parse_vehicle(html: str, url: str) -> Vehicle:
    """Build a Vehicle from a lot page's ``__NEXT_DATA__`` blob.

    Falls back to empty fields rather than raising — Troostwijk lots often
    ship a sparse attribute list (e.g. only Brand/Type/Mileage)."""
    data = _next_data(html)
    if not data:
        # No NEXT_DATA — return a stub so the URL isn't lost.
        return Vehicle(title="", url=url)

    lot = (((data.get("props") or {}).get("pageProps") or {}).get("lot")) or {}
    title = lot.get("title") or ""

    attrs = _attrs_to_dict(lot)
    brand = attrs.get("brand")
    model = attrs.get("model")
    year = attrs.get("year")
    first_reg = attrs.get("first_registration")
    if year is None and first_reg is not None:
        # Troostwijk frequently fills only one of Construction date / Date of
        # First Admission. Fall back so the scorer still sees a year.
        year = first_reg.year
    km = attrs.get("km")
    fuel = _normalize_fuel(attrs.get("fuel"))
    transmission = attrs.get("transmission")

    loc = lot.get("location") or {}
    city = loc.get("city")
    country = (loc.get("countryCode") or "").upper() or None
    location = ", ".join(p for p in [city, country] if p) or None

    start_ts = lot.get("startDate")
    auction_start = (
        datetime.fromtimestamp(start_ts, tz=timezone.utc) if isinstance(start_ts, (int, float)) else None
    )

    remarks = (lot.get("remarks") or "").strip() or None
    additional_information = (
        (((lot.get("description") or {}).get("additionalInformation")) or "").strip() or None
    )

    vehicle = Vehicle(
        title=title,
        brand=brand,
        model=model,
        url=url,
        source="troostwijk",
        platform=lot.get("platform"),
        year=year,
        first_registration=first_reg,
        km=km,
        fuel=fuel,
        transmission=transmission,
        power_kw=attrs.get("power_kw"),
        cylinder_cc=attrs.get("cylinder_cc"),
        emission_standard=attrs.get("emission_standard"),
        seats=attrs.get("seats"),
        doors=attrs.get("doors"),
        color=attrs.get("color"),
        weight_kg=attrs.get("weight_kg"),
        load_kg=attrs.get("load_kg"),
        vin=attrs.get("vin"),
        city=city,
        country_code=country,
        location=location,
        condition=lot.get("condition"),
        appearance=lot.get("appearance"),
        vat_margin=lot.get("marginGood"),
        bidding_status=lot.get("biddingStatus"),
        auction_start=auction_start,
        remarks=remarks,
        additional_information=additional_information,
    )

    ev = evaluate(vehicle)
    vehicle.passed_hard_filters = ev.passed_hard_filters
    vehicle.rejected_reason = ev.rejected_reason
    vehicle.size_class = ev.size_class
    vehicle.total_score = ev.total_score
    vehicle.reason_for_inclusion = ev.reasons
    vehicle.scores = ScoreBreakdown(**ev.scores) if ev.scores else None
    return vehicle


def crawl(query="Peugeot Boxer", pages=2):
    urls = get_lot_urls(query, pages=pages)
    results = []

    if not urls:
        return results

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for url in urls:
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
                v = parse_vehicle(page.content(), url)
                results.append(v.model_dump(mode="json"))
            except Exception as e:
                print(f"lot failed {url}: {e}")

        browser.close()

    return results
