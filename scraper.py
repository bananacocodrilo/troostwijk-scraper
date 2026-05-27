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

from models import Vehicle
from fleet import classify_fleet
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


def _clean_model(raw: Optional[str], brand: Optional[str]) -> Optional[str]:
    """Normalise the Troostwijk 'Type' attribute into a clean sub-model name.

    The raw value can be anything from a proper designation ("335 2.2 HDI L3H2")
    to a feature list ("- airco - euro 6b - trekhaak") or an internal code ("FCD").
    Returns None when the value carries no useful information.
    """
    if not raw:
        return None
    s = raw.strip()
    # Starts with "-" → features listed as model (Troostwijk data error)
    if s.startswith("-"):
        return None
    # Pure dash or blank
    if s in ("-", "--", "—", ""):
        return None
    # Duplicates the brand name (e.g. model="Peugeot" when brand="Peugeot")
    if brand and s.lower() == brand.lower():
        return None
    # Duplicates just the model token we already infer from the title
    # (e.g. model="Boxer" — already obvious from brand Peugeot)
    from van_intel import ALLOWED_MODELS
    if s.lower() in ALLOWED_MODELS:
        return None
    return s


def parse_vehicle(html: str, url: str) -> Vehicle:
    """Build a Vehicle from a lot page's ``__NEXT_DATA__`` blob.

    Falls back to empty fields rather than raising — Troostwijk lots often
    ship a sparse attribute list (e.g. only Brand/Type/Mileage)."""
    data = _next_data(html)
    if not data:
        return Vehicle(title="", url=url, rejected_reason="load_failed")

    lot = (((data.get("props") or {}).get("pageProps") or {}).get("lot")) or {}
    title = lot.get("title") or ""

    if not title:
        return Vehicle(title="", url=url, rejected_reason="load_failed")

    attrs = _attrs_to_dict(lot)
    brand = attrs.get("brand")
    raw_model = attrs.get("model")
    model = _clean_model(raw_model, brand)
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
        lot_id=lot.get("id"),
    )

    ev = evaluate(vehicle)
    vehicle.passed_hard_filters = ev.passed_hard_filters
    vehicle.is_valid_van = ev.passed_hard_filters
    vehicle.rejected_reason = ev.rejected_reason
    vehicle.van_type = ev.van_type
    vehicle.score = ev.score or 0
    vehicle.applied_rule_set = ev.applied_rule_set
    vehicle.reason_for_inclusion = ev.reasons
    vehicle.scores = ev.breakdown  # already a ScoreBreakdown (Pydantic), no conversion needed

    fleet_type, fleet_signals = classify_fleet(title, remarks, additional_information)
    vehicle.fleet_type = fleet_type
    vehicle.fleet_signals = fleet_signals or None
    return vehicle


def _parse_graphql_bid(payload: dict) -> Optional[dict]:
    """Extract bid fields from a storefront GraphQL response payload."""
    try:
        lot_info = payload.get("data", {}).get("lotDetails", {}).get("lot") or {}
        if not lot_info.get("id"):
            return None
        bid_cents = (lot_info.get("currentBidAmount") or {}).get("cents")
        if bid_cents is None:
            return None
        premium_raw = lot_info.get("markupPercentage")
        end_ts = lot_info.get("endDate")
        minimum_met = lot_info.get("minimumBidAmountMet")

        full_price = payload.get("data", {}).get("lotDetails", {}).get("estimatedFullPrice") or {}
        total_cents = (full_price.get("total") or {}).get("cents")

        return {
            "lot_id": lot_info["id"],
            "current_bid_eur": bid_cents / 100,
            "buyer_premium_pct": float(premium_raw) if premium_raw else None,
            "total_cost_eur": total_cents / 100 if total_cents else None,
            "bids_count": lot_info.get("bidsCount"),
            "reserve_met": (
                True if minimum_met is True else
                False if minimum_met is False else
                None if minimum_met is None else
                "MINIMUM_BID_AMOUNT_MET" in str(minimum_met)
            ),
            "auction_end_ts": end_ts,
        }
    except Exception:
        return None


def crawl(query="Peugeot Boxer", pages=2):
    urls = get_lot_urls(query, pages=pages)
    results = []

    if not urls:
        return results

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        # Intercept GraphQL responses that carry live bid data.
        bid_by_lot: dict = {}

        def on_response(response):
            if "storefront.tbauctions.com/storefront/graphql" not in response.url:
                return
            try:
                bid = _parse_graphql_bid(response.json())
                if bid:
                    bid_by_lot[bid["lot_id"]] = bid
            except Exception as e:
                print(f"graphql parse failed: {e}")

        page.on("response", on_response)

        for url in urls:
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
                v = parse_vehicle(page.content(), url)

                if v.lot_id and v.lot_id in bid_by_lot:
                    bd = bid_by_lot[v.lot_id]
                    v.current_bid_eur = bd["current_bid_eur"]
                    v.buyer_premium_pct = bd["buyer_premium_pct"]
                    v.total_cost_eur = bd["total_cost_eur"]
                    v.bids_count = bd["bids_count"]
                    v.reserve_met = bd["reserve_met"]
                    end_ts = bd.get("auction_end_ts")
                    if isinstance(end_ts, (int, float)):
                        v.auction_end = datetime.fromtimestamp(end_ts, tz=timezone.utc)

                results.append(v.model_dump(mode="json"))
            except Exception as e:
                print(f"lot failed {url}: {e}")

        browser.close()

    return results
