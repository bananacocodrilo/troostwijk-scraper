import json
import os

from scraper import crawl
from van_intel import SCORE_THRESHOLD

# Restricted to the four van platforms whose geometry suits camper conversion.
# Sprinter / Master / Crafter / Movano dropped per Phase 2 spec.
QUERIES = [
    "Fiat Ducato",
    "Peugeot Boxer",
    "Citroen Jumper",
    "Ford Transit",
]


def _dump(path: str, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def main():
    all_results = []
    seen_urls = set()

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

    accepted = [
        v for v in all_results
        if v.get("passed_hard_filters")
        and (v.get("total_score") or 0) >= SCORE_THRESHOLD
    ]
    rejected = [v for v in all_results if v not in accepted]

    accepted.sort(key=lambda v: v.get("total_score") or 0, reverse=True)

    _dump("output/latest.json", accepted)
    _dump("output/rejected.json", rejected)

    # Quick stats so the daily run leaves a readable log.
    reason_counts: dict = {}
    for v in rejected:
        r = (v.get("rejected_reason") or "unknown").split(":", 1)[0]
        reason_counts[r] = reason_counts.get(r, 0) + 1
    print(
        f"crawled={len(all_results)} accepted={len(accepted)} "
        f"rejected={len(rejected)} (threshold={SCORE_THRESHOLD})"
    )
    for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {r}: {n}")


if __name__ == "__main__":
    main()
