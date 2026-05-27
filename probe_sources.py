"""One-shot source-viability probe.

Visits candidate auction / classified sites with Playwright and records:
  - HTTP status, final URL, page size, page title
  - presence of __NEXT_DATA__ (Next.js SSR — easy to parse)
  - sample of links that look like lot/item/object URLs
  - whether a simple known-product search returns visible results

Writes a JSON report to output/source_probe.json so we can decide
whether each source is worth a full scraper without trial-and-error
in CI.

Usage: python probe_sources.py
"""

import json
import os
import re
import sys
from typing import Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# (name, url, lookup_query_or_none)
TARGETS = [
    ("klaravik-com",          "https://www.klaravik.com/",                                                None),
    ("klaravik-se-search",    "https://www.klaravik.se/auktioner?categories=transport-fordon",            "transport"),
    ("klaravik-en-vans",      "https://en.klaravik.com/auctions?category=transport",                      "transport"),
    ("2dehands-be-vans",      "https://www.2dehands.be/q/peugeot+boxer/",                                 "peugeot boxer"),
    ("2dehands-be-api",       "https://www.2dehands.be/lrp/api/search?query=peugeot+boxer&numberOfResultsPerPage=5", None),
    ("autoscout24-de-vans",   "https://www.autoscout24.de/transporter/peugeot/boxer",                     "boxer"),
    ("autoscout24-nl-vans",   "https://www.autoscout24.nl/bedrijfsauto/peugeot/boxer",                    "boxer"),
    ("bca-be-home",           "https://www.bca.be/",                                                      None),
    ("autorola-com-home",     "https://www.autorola.com/",                                                None),
    ("vavato-be-vans",        "https://www.vavato.com/en/c/transport-and-logistics",                      None),
]

LOT_LINK_RE = re.compile(r"/(lot|item|kavel|auction|object|p-|a-|annonce|ad/)|/[a-z]+/\d{4,}")


def probe_one(page, name: str, url: str, query: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "url": url}
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2_500)
        html = page.content()
    except Exception as e:
        return {**result, "error": str(e)[:150]}

    result["status"] = resp.status if resp else None
    result["final_url"] = page.url
    result["bytes"] = len(html)

    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    result["title"] = (title_tag.text.strip()[:120] if title_tag else "")
    result["has_next_data"] = bool(soup.find("script", {"id": "__NEXT_DATA__"}))
    result["has_apollo"] = "__APOLLO_STATE__" in html
    result["has_redux"] = "__REDUX_STATE__" in html or "__INITIAL_STATE__" in html

    # Sample lot-like links
    sample: list[str] = []
    for a in soup.select("a[href]")[:600]:
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if LOT_LINK_RE.search(href):
            if href not in sample:
                sample.append(href[:120])
            if len(sample) >= 5:
                break
    result["sample_lot_links"] = sample

    # If the page seems empty of content, mark it
    text = soup.get_text(" ", strip=True)
    result["text_len"] = len(text)
    if query:
        result["query_in_text"] = query.lower() in text.lower()

    # Cloudflare / bot wall sniff
    if any(s in html for s in ("cf-challenge", "Just a moment", "Checking your browser", "h-captcha")):
        result["bot_wall"] = True

    return result


def main():
    out_path = "output/source_probe.json"
    os.makedirs("output", exist_ok=True)
    report: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="nl-NL")
        page = ctx.new_page()
        for name, url, query in TARGETS:
            print(f"probing {name} …", file=sys.stderr)
            r = probe_one(page, name, url, query)
            report.append(r)
            print(f"  {name}: status={r.get('status')} title={r.get('title','')[:60]!r} "
                  f"next={r.get('has_next_data')} links={len(r.get('sample_lot_links',[]))} "
                  f"err={r.get('error','-')[:60]}", file=sys.stderr)
        browser.close()

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nwrote {out_path} with {len(report)} entries")


if __name__ == "__main__":
    main()
