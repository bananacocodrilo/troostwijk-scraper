"""Troostwijk lot scraper.

Pages are Next.js SSR — every spec field we need is in the ``__NEXT_DATA__``
JSON blob (``props.pageProps.lot``). We parse that directly instead of
chasing regex matches across rendered HTML.
"""

import json
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from models import Vehicle
from fleet import classify_fleet
from van_intel import SMALLER_SIBLINGS, evaluate

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


# Single-token smaller siblings — these reject from the slug. Multi-word
# entries from SMALLER_SIBLINGS (e.g. "transit connect") are matched as
# hyphenated substrings instead.
_SLUG_REJECT_TOKENS: set[str] = {
    # Smaller siblings already enumerated in van_intel.SMALLER_SIBLINGS.
    *(s for s in SMALLER_SIBLINGS if " " not in s),
    "jumpy",  # Citroen Jumpy — mid-size, not on our van whitelist
    # Peugeot passenger cars (skip "2008" — collides with year strings).
    "107", "108", "206", "207", "208", "306", "307", "308", "508",
    "605", "607", "806", "807", "1007", "3008", "4007", "4008", "5008",
    # Citroen passenger / vintage
    "c1", "c2", "c3", "c4", "c5", "c6", "ds3", "ds4", "ds5",
    "xsara", "picasso", "2cv",
    # VW passenger
    "golf", "polo", "passat", "jetta", "tiguan", "touareg", "touran", "t-roc",
    # Renault passenger
    "clio", "megane", "scenic", "captur", "twingo", "espace", "modus",
    # Opel passenger
    "corsa", "astra", "insignia", "meriva", "mokka", "crossland", "grandland",
    # Mercedes passenger / SUV (skip a/b/c/e/s-class — ambiguous with van trim codes).
    "glc", "gla", "gls", "glb", "gle", "cls",
    # Fiat passenger
    "panda", "punto", "tipo", "bravo", "stilo", "croma", "500l", "500x",
    # BMW SUVs (skip 1/3/5/7 series — ambiguous with year/load codes).
    "x1", "x3", "x5", "x6",
    # Audi
    "a1", "a3", "a4", "a5", "a6", "a7", "a8", "q2", "q3", "q5", "q7", "q8",
    # Two-wheelers and non-vehicle batches
    "moped", "mopeds", "scooter", "scooters", "bicycle", "bike",
    "motorcycle", "motorbike", "atv", "quad",
}
_SLUG_REJECT_PHRASES: list[str] = [
    "-".join(s.split()) for s in SMALLER_SIBLINGS if " " in s
] + [
    "tractor-parts", "timing-tool", "excavator-wheels",
]


def _url_looks_like_van(url: str) -> bool:
    """Cheap slug-only check to skip OBVIOUS non-vans before scraping.

    Policy: false positives (scraping a lot that turns out not to be a van)
    are cheap and acceptable — they let through occasional undervalued
    finds. Only reject when we are 100% sure: a known passenger-car or
    smaller-sibling model token is present in the slug. No allowlist
    enforcement here — some vans get listed without their canonical model
    name and would otherwise be wrongly filtered.
    """
    m = re.search(r"/l/([^/]+)-A\d+-\d+", url or "")
    if not m:
        return True
    slug = m.group(1).lower()
    tokens = set(slug.split("-"))
    if tokens & _SLUG_REJECT_TOKENS:
        return False
    if any(p in slug for p in _SLUG_REJECT_PHRASES):
        return False
    return True


def _collect_lot_urls_from_listing(page, listing_url: str) -> list[str]:
    """Navigate to a listing page (search results or category page) and
    return all lot URLs found, in document order. No deduping here — the
    caller manages dedup across multiple listing pages."""
    found: list[str] = []
    try:
        page.goto(listing_url, wait_until="networkidle", timeout=45_000)
    except Exception as e:
        print(f"listing page failed {listing_url}: {e}")
        return found
    soup = BeautifulSoup(page.content(), "html.parser")
    for a in soup.select("a[href*='/l/']"):
        href = a.get("href") or ""
        if href.startswith("/"):
            href = ORIGIN + href
        if not re.search(r"/l/[^/]+-A\d+-\d+", href):
            continue
        found.append(href)
    return found


def get_lot_urls(query, pages=1, year_min=None, year_max=None):
    """Collect lot URLs from a brand-keyword search."""
    urls: list[str] = []
    seen: set[str] = set()
    skipped_pre = 0

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for i in range(1, pages + 1):
            url = build_search_url(query, year_min, year_max, i)
            for href in _collect_lot_urls_from_listing(page, url):
                if href in seen:
                    continue
                seen.add(href)
                if not _url_looks_like_van(href):
                    skipped_pre += 1
                    continue
                urls.append(href)

        browser.close()

    if skipped_pre:
        print(f"  pre-filter: skipped {skipped_pre} URL(s) (known passenger-car / sibling token in slug)")

    return urls


def _category_page_url(base_url: str, page: int) -> str:
    """Build a paginated URL for a Troostwijk category. Strips any existing
    query string so we control page/pageSize ourselves."""
    base = base_url.split("?", 1)[0]
    return f"{base}?page={page}&pageSize=48"


def get_category_urls(category_url: str, pages: int = 3) -> list[str]:
    """Collect lot URLs from a Troostwijk category listing
    (e.g. Transport & Logistics → Trucks). Same slug pre-filter as the
    brand-search variant."""
    urls: list[str] = []
    seen: set[str] = set()
    skipped_pre = 0

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for i in range(1, pages + 1):
            page_url = _category_page_url(category_url, i)
            found = _collect_lot_urls_from_listing(page, page_url)
            if not found:
                # End of listings (or the page returned empty) — stop paginating.
                break
            new_this_page = 0
            for href in found:
                if href in seen:
                    continue
                seen.add(href)
                if not _url_looks_like_van(href):
                    skipped_pre += 1
                    continue
                urls.append(href)
                new_this_page += 1
            if new_this_page == 0:
                # Listing started repeating itself — no point continuing.
                break

        browser.close()

    if skipped_pre:
        print(f"  pre-filter: skipped {skipped_pre} URL(s) from category")

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


def crawl(query: str = "Peugeot Boxer", pages: int = 2, urls: Optional[list[str]] = None):
    """Scrape lot pages and return a list of Vehicle dicts.

    If `urls` is provided, those are scraped directly (skip the search
    step). Otherwise URLs are collected from a brand-keyword search for
    `query`. Use `urls=…` when you want to combine multiple sources
    (brand search + category page + …) and dedupe upstream.
    """
    if urls is None:
        urls = get_lot_urls(query, pages=pages)
    results = []
    missing_bid_data = []

    if not urls:
        return results

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        # Intercept GraphQL responses that carry live bid data. The bid widget
        # on every TB-Auctions storefront (Troostwijk, Vavato, BVA, …) calls a
        # `/storefront/graphql` endpoint — match by path so we catch every
        # property-specific host (storefront.tbauctions.com,
        # storefront.vavato.com, etc.) rather than just the umbrella one.
        bid_by_lot: dict = {}

        def on_response(response):
            if "/storefront/graphql" not in response.url:
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

                # The bid GraphQL call sometimes lands AFTER networkidle
                # (e.g. when the bid widget hydrates a beat later). Poll a
                # few seconds before giving up so we don't drop the lot's
                # current_bid / auction_end / bids_count.
                if v.lot_id and v.lot_id not in bid_by_lot:
                    deadline = time.monotonic() + 8.0
                    while time.monotonic() < deadline and v.lot_id not in bid_by_lot:
                        page.wait_for_timeout(250)

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
                elif v.lot_id:
                    missing_bid_data.append((v.platform, url))
                    print(f"  ⚠️  no bid GraphQL captured for {v.platform} lot {v.lot_id} ({url})")

                results.append(v.model_dump(mode="json"))
            except Exception as e:
                print(f"lot failed {url}: {e}")

        browser.close()

    if missing_bid_data:
        print(f"\n⚠️  {len(missing_bid_data)} lot(s) finished without bid GraphQL:")
        for plat, u in missing_bid_data:
            print(f"   - {plat}: {u}")

    return results
