import re
from datetime import datetime

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from models import Vehicle
from van_intel import detect_size, is_hidden_gem, is_valid_van, score_vehicle

BASE = "https://www.troostwijkauctions.com/en/search"
ORIGIN = "https://www.troostwijkauctions.com"

CURRENT_YEAR = datetime.now().year

# Labels Troostwijk uses on lot detail pages (EN + NL).
YEAR_LABELS = ("year of construction", "year", "bouwjaar")
KM_LABELS = ("mileage", "kilometre", "kilometers", "kilometer", "kilometerstand", "km stand")
LOCATION_LABELS = ("location", "locatie", "viewing location", "kijklocatie")


def build_search_url(query, year_min=None, year_max=None, page=1):
    url = f"{BASE}?page={page}&pageSize=48&searchTerm={query}&sort=relevance"
    if year_min and year_max:
        url += f"&yearsBuilt={year_min}%2C{year_max}"
    return url


def _new_browser_page(p):
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
        browser, context = _new_browser_page(p)
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
                # Lot URLs look like .../l/<slug>-A<auctionId>-<lotId>
                if not re.search(r"/l/[^/]+-A\d+-\d+", href):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                urls.append(href)

        browser.close()

    return urls


def _extract_int(text):
    if not text:
        return None
    nums = re.findall(r"\d[\d\.]*", text.replace(".", "").replace(",", ""))
    return int(nums[0]) if nums else None


def _label_value(soup, labels):
    """Find the textual value adjacent to one of the labels (dt/dd, th/td, or sibling spans)."""
    for label in labels:
        rx = re.compile(rf"^\s*{re.escape(label)}\s*:?\s*$", re.IGNORECASE)
        node = soup.find(string=rx)
        if not node:
            # also accept labels that start with the keyword (e.g. "Mileage (km)")
            rx2 = re.compile(rf"^\s*{re.escape(label)}\b", re.IGNORECASE)
            node = soup.find(string=rx2)
        if not node:
            continue
        parent = node.parent
        if not parent:
            continue
        # dt -> dd
        if parent.name == "dt":
            dd = parent.find_next_sibling("dd")
            if dd:
                return dd.get_text(" ", strip=True)
        # th -> td
        if parent.name == "th":
            td = parent.find_next_sibling("td")
            if td:
                return td.get_text(" ", strip=True)
        # span/div labelled cell: look at the parent row's siblings
        for sib in parent.next_siblings:
            if getattr(sib, "get_text", None):
                txt = sib.get_text(" ", strip=True)
                if txt:
                    return txt
        # fallback: parent text minus the label itself
        whole = parent.get_text(" ", strip=True)
        cleaned = re.sub(rf"^{re.escape(label)}\s*:?\s*", "", whole, flags=re.IGNORECASE)
        if cleaned and cleaned.lower() != whole.lower():
            return cleaned
    return None


def _guess_brand_model(title):
    if not title:
        return None, None
    tokens = [t for t in re.split(r"[\s\-–—|]+", title) if t and t != "-"]
    if len(tokens) >= 2:
        return tokens[0], tokens[1]
    if tokens:
        return tokens[0], None
    return None, None


def _extract_year(soup, full_text):
    raw = _label_value(soup, YEAR_LABELS)
    if raw:
        m = re.search(r"(19|20)\d{2}", raw)
        if m:
            y = int(m.group(0))
            if 1990 <= y <= CURRENT_YEAR:
                return y
    # Fallback: any plausible year that isn't the current year (copyright/footer leak).
    for match in re.finditer(r"(19|20)\d{2}", full_text):
        y = int(match.group(0))
        if 1990 <= y < CURRENT_YEAR:
            return y
    return None


def _extract_km(soup, full_text):
    raw = _label_value(soup, KM_LABELS)
    if raw:
        n = _extract_int(raw)
        if n and 1_000 <= n <= 1_500_000:
            return n
    m = re.search(r"(\d[\d\.\,]{2,})\s?km\b", full_text, re.IGNORECASE)
    if m:
        n = _extract_int(m.group(1))
        if n and 1_000 <= n <= 1_500_000:
            return n
    return None


def _extract_fuel(full_text):
    lower = full_text.lower()
    if "diesel" in lower:
        return "diesel"
    if "benzine" in lower or "petrol" in lower or "gasoline" in lower:
        return "petrol"
    if "electric" in lower or "elektrisch" in lower:
        return "electric"
    return None


def _extract_location(soup):
    raw = _label_value(soup, LOCATION_LABELS)
    if not raw:
        return None
    # Trim noisy trailing labels.
    return raw.split("\n")[0].strip() or None


def parse_vehicle(html, url):
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h1")
    title = title_el.text.strip() if title_el else ""

    brand, model = _guess_brand_model(title)
    text = soup.get_text(" ", strip=True)

    year = _extract_year(soup, text)
    km = _extract_km(soup, text)
    fuel = _extract_fuel(text)
    location = _extract_location(soup)

    valid = is_valid_van(title)
    van_type = detect_size(title) or detect_size(text)
    score = score_vehicle(year, km, van_type, fuel) if valid else 0
    gem = is_hidden_gem(score, year, km) if valid else False

    return Vehicle(
        title=title,
        brand=brand,
        model=model,
        year=year,
        km=km,
        fuel=fuel,
        location=location,
        url=url,
        source="troostwijk",
        van_type=van_type,
        is_valid_van=valid,
        score=score,
        hidden_gem=gem,
    )


def crawl(query="Peugeot Boxer", pages=2):
    """Crawl a single search query end-to-end, reusing one browser for all lot fetches."""
    urls = get_lot_urls(query, pages=pages)
    results = []

    if not urls:
        return results

    with sync_playwright() as p:
        browser, context = _new_browser_page(p)
        page = context.new_page()

        for url in urls:
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
                v = parse_vehicle(page.content(), url)
                results.append(v.model_dump())
            except Exception as e:
                print(f"lot failed {url}: {e}")

        browser.close()

    return results
