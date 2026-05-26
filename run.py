import json
import os
from scraper import crawl

QUERIES = [
    "Peugeot Boxer",
    "Fiat Ducato",
    "Citroen Jumper",
    "Ford Transit",
    "Mercedes Sprinter",
    "Renault Master",
    "Volkswagen Crafter",
    "Opel Movano",
]


def main():
    all_results = []
    seen_urls = set()

    for query in QUERIES:
        print(f"Crawling: {query}")
        try:
            data = crawl(query, pages=2)
        except Exception as e:
            print(f"query failed: {query}: {e}")
            continue

        for v in data:
            if v["url"] in seen_urls:
                continue
            seen_urls.add(v["url"])
            all_results.append(v)

    vans = [v for v in all_results if v.get("is_valid_van")]
    vans.sort(key=lambda v: v.get("score", 0), reverse=True)

    os.makedirs("output", exist_ok=True)
    with open("output/latest.json", "w") as f:
        json.dump(vans, f, indent=2)

    gems = sum(1 for v in vans if v.get("hidden_gem"))
    print(f"Saved {len(vans)} vans ({gems} hidden gems) from {len(all_results)} total listings")


if __name__ == "__main__":
    main()
