import json
from scraper import crawl

def main():
    data = crawl("Peugeot Boxer", pages=2)

    with open("output/latest.json", "w") as f:
        json.dump(data, f, indent=2)

    print(f"Saved {len(data)} vehicles")

if __name__ == "__main__":
    main()
