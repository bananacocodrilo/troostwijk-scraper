import json
import os

import bid_history
from cost_model import DEFAULT_BUYER_PREMIUM, compute_costs, passes_cost_filter
from marktplaats import build_price_index
from notify import notify_gems
from scraper import VAVATO_BASE, crawl_parallel, get_category_urls, get_lot_urls
from van_intel import ALLOWED_MODELS, SCORE_THRESHOLD

MAX_BID_TARGET_FRACTION = 0.65

QUERIES = [
    "Peugeot Boxer",
    "Citroen Jumper",
    "Fiat Ducato",
    "Mercedes Sprinter",
    "Ford Transit",
    "Renault Master",
    "Volkswagen Crafter",
    "Opel Movano",
    "MAN TGE",
    "Iveco Daily",
    "Peugeot Expert",
    "Volkswagen Transporter",
]

# Troostwijk category pages. Drilled down to "Vans" specifically inside the
# Cars taxonomy (~274 listings) rather than the parent "Cars" category
# (~2017 listings, mostly passenger cars). Trucks-and-trailers is kept for
# heavier vans / box trucks that get bucketed there.
CATEGORIES: list[tuple[str, str]] = [
    (
        "trucks-and-trailers",
        "https://www.troostwijkauctions.com/en/c/transport-logistics/trucks-trailers/fd5500c7-5590-42fb-8f0b-24fa8e6d95da",
    ),
    (
        "vans",
        "https://www.troostwijkauctions.com/en/c/transport/cars/5196727d-c14f-48dc-a2f0-e75f50094a52?categoryLevel3=b3ee855f-3320-4b3c-895c-fbf321f401d6",
    ),
]
# 10 × 48 = 480 listings per category; well above current vans count (274)
# and stops early on the first empty page anyway.
CATEGORY_PAGES = 10
BRAND_PAGES = 2


def _model_key(title: str) -> str:
    s = (title or "").lower()
    for token in ALLOWED_MODELS:
        if token in s:
            return token
    return ""


def _dump(path: str, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def main():
    # 1. Collect lot URLs from every source first, then scrape once. The
    #    scrape step is the expensive bit, so deduping URLs upstream
    #    avoids re-fetching lots that appear under multiple sources.
    all_urls: list[str] = []
    seen_urls: set[str] = set()

    def _add(label: str, urls: list[str]) -> int:
        new = 0
        for u in urls:
            if u in seen_urls:
                continue
            seen_urls.add(u)
            all_urls.append(u)
            new += 1
        print(f"  {label}: +{new} new (collected {len(urls)})")
        return new

    print("Collecting URLs from category pages:")
    for label, cat_url in CATEGORIES:
        try:
            _add(label, get_category_urls(cat_url, pages=CATEGORY_PAGES))
        except Exception as e:
            print(f"  category {label} failed: {e}")

    print("Collecting URLs from brand searches:")
    for query in QUERIES:
        try:
            _add(query, get_lot_urls(query, pages=BRAND_PAGES))
        except Exception as e:
            print(f"  query {query} failed: {e}")

    # Vavato shares the TB-Auctions storefront but has its own inventory
    # (BVA was merged into Troostwijk so it's already covered above).
    print("Collecting URLs from Vavato brand searches:")
    for query in QUERIES:
        try:
            _add(f"vavato:{query}", get_lot_urls(query, pages=BRAND_PAGES, base=VAVATO_BASE))
        except Exception as e:
            print(f"  vavato query {query} failed: {e}")

    print(f"\nTotal unique URLs to scrape: {len(all_urls)}")

    # 2. Scrape each lot once. Parallelised across 4 browser contexts —
    #    network-bound, so threads share CPU well and we get ~3× wall-time
    #    speedup vs serial.
    all_results = crawl_parallel(all_urls, workers=4)

    # 2a. Persist a bid-history snapshot of every scraped lot. Finalises
    #     hammer prices on auctions whose end has passed. Used later as
    #     a higher-priority market reference than Marktplaats once we
    #     have enough closed-auction samples per model/year bucket.
    bid_history.update(all_results, model_token_of=_model_key)
    hammer_index = bid_history.load_index()

    # 2b. Marktplaats price index
    print("\nBuilding Marktplaats price index...")
    price_index = build_price_index(QUERIES)

    # 3. Attach market data + compute true cost + re-filter
    accepted = []
    cost_rejected = []
    suitability_rejected = []

    for v in all_results:
        # First gate: suitability hard filters + score threshold
        if not v.get("passed_hard_filters") or (v.get("score") or 0) < SCORE_THRESHOLD:
            suitability_rejected.append(v)
            continue

        mk = _model_key(v.get("title", ""))
        year = v.get("year")

        # Attach Marktplaats data
        median = price_index.median(mk, year)
        sample = price_index.sample_size(mk, year)
        v["market_median_eur"] = median
        v["market_sample_size"] = sample

        # Attach hammer-history data — preferred source in cost_model
        # when sample is large enough.
        v["hammer_median_eur"] = hammer_index.median(mk, year)
        v["hammer_sample_size"] = hammer_index.sample_size(mk, year)

        # Legacy deal margin (kept for backwards compat)
        total_cost = v.get("total_cost_eur")
        if median and total_cost:
            margin = round(median - total_cost)
            v["deal_margin_eur"] = margin
            v["deal_margin_pct"] = round(margin / median * 100, 1)

        if median:
            premium = 1 + (v.get("buyer_premium_pct") or DEFAULT_BUYER_PREMIUM * 100) / 100
            v["max_recommended_bid_eur"] = round(median * MAX_BID_TARGET_FRACTION / premium)

        # True cost model
        cost_fields = compute_costs(v, model_token=mk)
        v.update(cost_fields)

        # Cost filter (overpaying vs market, or too expensive to recondition)
        passes, cost_reason = passes_cost_filter(v)
        if not passes:
            v["rejected_reason"] = cost_reason
            cost_rejected.append(v)
            continue

        # cost_model adds deal_score separately — do NOT overwrite score.
        # score = suitability (always matches ScoreBreakdown fields).
        # deal_score = financial quality (deal_ratio-derived, shown separately).

        accepted.append(v)

    rejected = suitability_rejected + cost_rejected
    accepted.sort(key=lambda v: v.get("score") or 0, reverse=True)

    _dump("output/latest.json", accepted)
    _dump("output/rejected.json", rejected)

    reason_counts: dict = {}
    for v in rejected:
        r = (v.get("rejected_reason") or "unknown").split(":", 1)[0]
        reason_counts[r] = reason_counts.get(r, 0) + 1

    load_failures = reason_counts.pop("load_failed", 0)
    gems = [v for v in accepted if v.get("is_hidden_gem")]
    print(
        f"\ncrawled={len(all_results)} accepted={len(accepted)} "
        f"load_failed={load_failures} filtered={len(rejected) - load_failures}"
    )
    for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {r}: {n}")

    if gems:
        print(f"\n💎 {len(gems)} hidden gems:")
        for v in gems[:5]:
            ratio = v.get("deal_ratio") or 0
            print(
                f"  {v['title'][:45]:45}"
                f"  final=€{v.get('final_cost_estimate') or 0:,.0f}"
                f"  market=€{v.get('estimated_market_value') or 0:,.0f}"
                f"  ratio={ratio:+.0%}"
            )

    # Telegram alerts for hidden gems closing within 24h.
    sent = notify_gems(accepted)
    if sent:
        print(f"\n📨 sent {sent} Telegram alert(s)")

    # Fleet provenance breakdown — informational only
    fleet_counts: dict = {}
    for v in accepted:
        ft = v.get("fleet_type") or "private"
        fleet_counts[ft] = fleet_counts.get(ft, 0) + 1
    if fleet_counts:
        print("\nfleet types (accepted):")
        for ft, n in sorted(fleet_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {ft}: {n}")


if __name__ == "__main__":
    main()
