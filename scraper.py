from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re
from models import Vehicle

BASE = "https://www.troostwijkauctions.com/en/search"

def build_search_url(query, year_min=None, year_max=None, page=1):
    url = f"{BASE}?page={page}&pageSize=48&searchTerm={query}&sort=relevance"
    if year_min and year_max:
        url += f"&yearsBuilt={year_min}%2C{year_max}"
    return url

def get_lot_urls(query, pages=1, year_min=None, year_max=None):
    urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for i in range(1, pages + 1):
            url = build_search_url(query, year_min, year_max, i)
            page.goto(url, wait_until="networkidle")

            soup = BeautifulSoup(page.content(), "html.parser")

            for a in soup.select("a[href*='/l/']"):
                href = a.get("href")
                if href and href.startswith("/"):
                    href = "https://www.troostwijkauctions.com" + href
                if href and href not in urls:
                    urls.append(href)

        browser.close()

    return urls

def extract_int(text):
    if not text:
        return None
    nums = re.findall(r"\d[\d\.]*", text.replace(".", ""))
    return int(nums[0]) if nums else None

def guess_brand_model(title):
    if not title:
        return None, None
    parts = title.split()
    return (parts[0], parts[1]) if len(parts) >= 2 else (None, None)

def parse_vehicle(html, url):
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h1")
    title = title_el.text.strip() if title_el else ""

    brand, model = guess_brand_model(title)

    text = soup.get_text(" ", strip=True)

    year = None
    y = re.search(r"(19|20)\d{2}", text)
    if y:
        year = int(y.group(0))

    km = None
    km_match = re.search(r"(\d[\d\.]+)\s?km", text.lower())
    if km_match:
        km = extract_int(km_match.group(0))

    fuel = None
    if "diesel" in text.lower():
        fuel = "diesel"
    elif "benzine" in text.lower() or "petrol" in text.lower():
        fuel = "petrol"

    return Vehicle(
        title=title,
        brand=brand,
        model=model,
        year=year,
        km=km,
        fuel=fuel,
        transmission=None,
        location=None,
        url=url,
        auction_end=None,
        source="troostwijk"
    )

def fetch_vehicle(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        html = page.content()
        browser.close()

    return parse_vehicle(html, url)

def crawl(query="Peugeot Boxer", pages=2):
    results = []
    urls = get_lot_urls(query, pages=pages)

    for url in urls:
        try:
            v = fetch_vehicle(url)
            results.append(v.model_dump())
        except Exception as e:
            print("fail", url, e)

    return results
