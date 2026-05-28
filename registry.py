"""Lot registry — discovery + priority-refresh state.

Goal: avoid re-scraping every lot on every run. We discover URLs cheaply
on every run (listing pages only), but the expensive per-lot detail
scrape is bucketed by ``auction_end`` distance:

  • <24h to close  → refresh every run    (so closing-soon alerts stay fresh)
  • 24-72h         → refresh every other run (~every 12h at 6h cadence)
  • >72h or unknown → refresh once per day

New URLs (never seen) are always scraped. Stale entries (already in
registry, not scraped this run) keep their last-known Vehicle dump so
``latest.json`` always reflects the union of fresh + stale data.

State file: ``output/lot_registry.json``, committed alongside the other
outputs so the registry survives across GH Actions runs."""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

PATH = "output/lot_registry.json"

# Hours since last scrape required before re-scraping at each priority tier.
REFRESH_MAX_AGE_H = {
    "closing_soon":  0,    # always refresh (<24h to close)
    "soon":          8,    # ~every other 6h run (24-72h)
    "later":         22,   # once per day (>72h)
    "unknown":      12,    # twice per day when end-date missing
    "ended":      9999,    # never re-scrape once closed
}

# Drop entries whose auction ended this many days ago — bid_history has
# already captured the final hammer and the lot URL likely 404s.
PRUNE_DAYS_AFTER_END = 7


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load(path: str = PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lots": {}}


def save(data: dict, path: str = PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _priority_tier(entry: dict, now: datetime) -> str:
    """Classify an entry by closeness to auction_end."""
    end = _parse_dt(entry.get("auction_end"))
    if end is None:
        return "unknown"
    delta_h = (end - now).total_seconds() / 3600
    if delta_h < 0:
        return "ended"
    if delta_h < 24:
        return "closing_soon"
    if delta_h < 72:
        return "soon"
    return "later"


def should_scrape(entry: Optional[dict], now: datetime) -> bool:
    """True when this URL needs a fresh detail-scrape this run."""
    if entry is None:
        return True
    last = _parse_dt(entry.get("last_scrape_at"))
    if last is None:
        return True
    tier = _priority_tier(entry, now)
    max_age = REFRESH_MAX_AGE_H.get(tier, 12)
    age_h = (now - last).total_seconds() / 3600
    return age_h >= max_age


def select_urls_to_scrape(discovered_urls: Iterable[str],
                          registry: dict,
                          *, now: Optional[datetime] = None) -> tuple[list[str], dict]:
    """Split ``discovered_urls`` into the subset that needs a fresh
    detail-scrape. Returns ``(urls_to_scrape, stats)`` where stats has
    per-tier counts for logging."""
    now = now or datetime.now(timezone.utc)
    lots = registry.get("lots", {})
    to_scrape: list[str] = []
    stats = {"new": 0, "closing_soon": 0, "soon": 0, "later": 0, "unknown": 0, "ended": 0, "skipped": 0}
    for url in discovered_urls:
        entry = lots.get(url)
        if entry is None:
            stats["new"] += 1
            to_scrape.append(url)
        elif should_scrape(entry, now):
            stats[_priority_tier(entry, now)] += 1
            to_scrape.append(url)
        else:
            stats["skipped"] += 1
    return to_scrape, stats


def merge(registry: dict, fresh_results: Iterable[dict],
          *, now: Optional[datetime] = None) -> dict:
    """Update the registry in-place with this run's fresh scrape results.
    Skips entries that came back as load_failed (no title / no useful
    data) so a transient fetch failure doesn't wipe the previous
    known-good snapshot — the URL stays in its current registry tier
    and will be re-tried on the next eligible run.

    Returns the registry for chaining."""
    now = (now or datetime.now(timezone.utc)).isoformat()
    lots = registry.setdefault("lots", {})
    for v in fresh_results:
        url = v.get("url")
        if not url:
            continue
        if not v.get("title") or v.get("rejected_reason") == "load_failed":
            continue
        lots[url] = {
            **v,
            "last_scrape_at": now,
        }
    return registry


def prune(registry: dict, *, now: Optional[datetime] = None) -> int:
    """Drop entries whose auction ended more than ``PRUNE_DAYS_AFTER_END``
    days ago. Returns the count removed."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=PRUNE_DAYS_AFTER_END)
    lots = registry.get("lots", {})
    drop = []
    for url, entry in lots.items():
        end = _parse_dt(entry.get("auction_end"))
        if end is not None and end < cutoff:
            drop.append(url)
    for url in drop:
        del lots[url]
    return len(drop)


def all_known_vehicles(registry: dict) -> list[dict]:
    """Return every vehicle dict in the registry (fresh + stale) — the
    union that ``run.py`` runs cost/filter/notification over."""
    return list(registry.get("lots", {}).values())
