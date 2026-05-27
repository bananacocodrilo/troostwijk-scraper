import json
import os

from cost_model import DEFAULT_BUYER_PREMIUM, compute_costs, passes_cost_filter
from marktplaats import build_price_index
from scraper import crawl
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
    # 1. Crawl
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

    # 2. Marktplaats price index
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
