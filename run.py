import json
import os

from marktplaats import build_price_index
from scraper import crawl
from van_intel import ALLOWED_MODELS, SCORE_THRESHOLD

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
]


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
    # 1. Crawl Troostwijk (includes bid interception via GraphQL).
    all_results = []
    seen_urls: set = set()

    for query in QUERIES:
        print(f"Crawling: {query}")
        try:
            data = crawl(query, pages=2)
        except Exception as e:
            print(f"  query failed: {e}")
            continue
        for v in data:
            if v["url"] in seen_urls:
                continue
            seen_urls.add(v["url"])
            all_results.append(v)

    # 2. Build Marktplaats retail price index.
    print("\nBuilding Marktplaats price index...")
    price_index = build_price_index(QUERIES)

    # 3. Split accepted / rejected and annotate accepted with market data.
    accepted = []
    rejected = []

    for v in all_results:
        if v.get("passed_hard_filters") and (v.get("score") or 0) >= SCORE_THRESHOLD:
            mk = _model_key(v.get("title", ""))
            year = v.get("year")
            median = price_index.median(mk, year)
            sample = price_index.sample_size(mk, year)

            v["market_median_eur"] = median
            v["market_sample_size"] = sample

            total_cost = v.get("total_cost_eur")
            if median and total_cost:
                margin = round(median - total_cost)
                v["deal_margin_eur"] = margin
                v["deal_margin_pct"] = round(margin / median * 100, 1)

            accepted.append(v)
        else:
            rejected.append(v)

    accepted.sort(key=lambda v: v.get("score") or 0, reverse=True)

    # 4. Write output.
    _dump("output/latest.json", accepted)
    _dump("output/rejected.json", rejected)

    reason_counts: dict = {}
    for v in rejected:
        r = (v.get("rejected_reason") or "unknown").split(":", 1)[0]
        reason_counts[r] = reason_counts.get(r, 0) + 1

    print(
        f"\ncrawled={len(all_results)} accepted={len(accepted)} "
        f"rejected={len(rejected)} (threshold={SCORE_THRESHOLD})"
    )
    for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {r}: {n}")

    gems = [v for v in accepted if (v.get("deal_margin_pct") or 0) >= 20]
    if gems:
        print(f"\n{len(gems)} lots with deal_margin >= 20%:")
        for v in gems[:5]:
            print(
                f"  {v['title'][:45]:45} score={v.get('total_score'):.1f}"
                f"  bid=€{v.get('current_bid_eur'):,.0f}"
                f"  total=€{v.get('total_cost_eur'):,.0f}"
                f"  market=€{v.get('market_median_eur'):,.0f}"
                f"  margin={v.get('deal_margin_pct'):+.0f}%"
            )


if __name__ == "__main__":
    main()
