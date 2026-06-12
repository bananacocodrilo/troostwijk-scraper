"""Lot registry — discovery + priority-refresh state + reject cache.

Goal: avoid re-scraping every lot on every run. We discover URLs cheaply
on every run (listing pages only), but the expensive per-lot detail
scrape is bucketed by ``auction_end`` distance:

  • <24h to close  → refresh every run    (so closing-soon alerts stay fresh)
  • 24-72h         → refresh every other run (~every 12h at 6h cadence)
  • >72h or unknown → refresh once per day

New URLs (never seen) are always scraped, but capped at
``MAX_NEW_PER_RUN`` per invocation so the first few runs after a clean
start don't blow past the CI timeout. The deferred URLs aren't lost —
they remain "new" on the next run and get picked up via random sample
until the catalogue is fully registered (typically 3-4 runs).

Stale entries (already in registry, not scraped this run) keep their
last-known Vehicle dump so ``latest.json`` always reflects the union of
fresh + stale data.

URLs whose first scrape returns a permanent rejection reason (it's not
a van, it's too old, it's a tipper, …) are recorded in
``permanent_rejects`` and skipped on every future discovery pass. Only
rejection reasons whose verdict can't change get cached this way —
transient rejections (score below threshold, market shifted) stay in
``lots`` and get re-evaluated.

State file: ``output/lot_registry.json``, committed alongside the other
outputs so the registry survives across GH Actions runs."""

import json
import os
import random
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

# Drop entries whose auction ended more than this many days ago — bid_history
# has already captured the final hammer and the lot URL likely 404s.
PRUNE_DAYS_AFTER_END = 7

# Cap on never-seen URLs scraped per run, so a cold-start scrape (empty
# registry) doesn't try to chew through the entire catalogue at once and
# hit the CI timeout. Deferred URLs remain "new" next run and get picked
# up by random sampling — the catalogue typically becomes fully
# registered within 3-4 runs at this cap.
MAX_NEW_PER_RUN = 400

# Rejection reasons whose verdict won't change on re-scrape: they reflect
# stable lot attributes (it's not a van, it's too old, it's a tipper, …)
# rather than transient market state (bid moved, scoring rule shifted).
# A URL that fails for one of these reasons gets cached so we don't waste
# a scrape budget on it every run.
PERMANENT_REJECT_PREFIXES = (
    "brand_not_whitelisted",
    "brand_not_in_whitelist",
    "smaller_sibling",
    "vehicle_type",
    "body_mismatch",
    "damage",
    "size_too_small",
    "size_not_allowed",
    "mileage_too_high",
    "year_below_minimum",
    "emission_below_euro6",
    "seats_below_5",
    # Manual dismissal from the dashboard (user_overrides.json). Reconciled
    # every run by apply_user_overrides(): added when a URL is dismissed,
    # removed when it is un-dismissed.
    "user_dismissed",
    # Legacy reason kept so URLs rejected by the pre-2026 (`< 6`) gate
    # stay permanently-rejected and don't get re-scraped.
    "seats_below_6",
)

# Reject reason recorded for lots the user manually dismissed in the
# dashboard. Kept as a named constant so apply_user_overrides() can find
# and reconcile exactly these entries without touching real rejections.
USER_DISMISSED_REASON = "user_dismissed"


def _is_permanent_reject(reason: Optional[str]) -> bool:
    if not reason:
        return False
    prefix = reason.split(":", 1)[0].strip()
    return prefix in PERMANENT_REJECT_PREFIXES


# Reasons whose verdict depends on the current size rules (group
# required_length / required_height, weight-band thresholds, keyword
# inference). Whenever any of those change in van_intel.py, the cached
# rejections may now be wrong — call ``clear_rejects_by_reason()`` with
# these prefixes so the affected URLs get rescraped on the next run.
SIZE_REJECT_PREFIXES = (
    "size_not_allowed",
    "size_too_small",
)


def clear_rejects_by_reason(registry: dict, reason_prefixes: Iterable[str]) -> list[str]:
    """Drop permanent_rejects whose reason starts with any of the given
    prefixes. Returns the list of cleared URLs.

    Call after rule changes in van_intel.py (size, year, emission, seats,
    etc.) so URLs that would now pass under the new rules get a fresh
    scrape instead of being eternally cached as 'no thanks'.
    """
    rejects = registry.get("permanent_rejects", {})
    prefixes = tuple(reason_prefixes)
    cleared = []
    for url in list(rejects.keys()):
        reason = (rejects[url].get("reason") or "").split(":", 1)[0].strip()
        if reason in prefixes:
            cleared.append(url)
            del rejects[url]
    return cleared


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
                          *, now: Optional[datetime] = None,
                          max_new: int = MAX_NEW_PER_RUN) -> tuple[list[str], dict]:
    """Split ``discovered_urls`` into the subset that needs a fresh
    detail-scrape this run. Returns ``(urls_to_scrape, stats)``.

    Priority refreshes (closing_soon / soon / later / unknown tiers on
    already-registered URLs) are always included — they have time
    pressure tied to ``auction_end``.

    Never-seen URLs are capped at ``max_new`` per run via random
    sampling. The deferred URLs aren't tracked anywhere; they'll show
    up as "new" again on the next discovery pass and have an equal
    chance of being sampled."""
    now = now or datetime.now(timezone.utc)
    lots = registry.get("lots", {})
    rejects = registry.get("permanent_rejects", {})
    refresh: list[str] = []
    new_urls: list[str] = []
    stats = {"new": 0, "new_deferred": 0, "closing_soon": 0, "soon": 0,
             "later": 0, "unknown": 0, "ended": 0, "skipped": 0, "perm_reject": 0}
    for url in discovered_urls:
        if url in rejects:
            stats["perm_reject"] += 1
            continue
        entry = lots.get(url)
        if entry is None:
            new_urls.append(url)
        elif should_scrape(entry, now):
            stats[_priority_tier(entry, now)] += 1
            refresh.append(url)
        else:
            stats["skipped"] += 1

    if len(new_urls) > max_new:
        sampled = random.sample(new_urls, max_new)
        stats["new"] = max_new
        stats["new_deferred"] = len(new_urls) - max_new
    else:
        sampled = new_urls
        stats["new"] = len(new_urls)

    return refresh + sampled, stats


def merge(registry: dict, fresh_results: Iterable[dict],
          *, now: Optional[datetime] = None) -> dict:
    """Update the registry in-place with this run's fresh scrape results.

    Behaviour by result kind:
      • load_failed / empty title  → skip, leave previous snapshot intact
      • permanent reject reason    → record in ``permanent_rejects`` cache
                                     and remove from ``lots`` so we never
                                     re-scrape this URL
      • accepted / transient reject → store full Vehicle dump in ``lots``

    Returns the registry for chaining."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    lots = registry.setdefault("lots", {})
    rejects = registry.setdefault("permanent_rejects", {})
    for v in fresh_results:
        url = v.get("url")
        if not url:
            continue
        if not v.get("title") or v.get("rejected_reason") == "load_failed":
            continue
        reason = v.get("rejected_reason")
        if _is_permanent_reject(reason):
            rejects[url] = {
                "reason": reason,
                "title": v.get("title"),
                "rejected_at": now_iso,
            }
            lots.pop(url, None)
            continue
        lots[url] = {
            **v,
            "last_scrape_at": now_iso,
        }
    return registry


def prune(registry: dict, *, now: Optional[datetime] = None) -> int:
    """Drop ``lots`` entries whose auction ended more than
    ``PRUNE_DAYS_AFTER_END`` days ago. Returns the count removed.

    ``permanent_rejects`` are not pruned — they're cheap (one line per
    URL) and we want the cache to remain authoritative across the entire
    lifespan of the project."""
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


# ---------------------------------------------------------------------------
# User overrides (dashboard dismiss / bookmark → output/user_overrides.json)
# ---------------------------------------------------------------------------

USER_OVERRIDES_PATH = "output/user_overrides.json"


def load_user_overrides(path: str = USER_OVERRIDES_PATH) -> dict:
    """Load the dashboard's dismiss/bookmark file. Tolerant of a missing or
    corrupt file — returns an empty structure so the pipeline never breaks."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"dismissed": {}, "bookmarked": {}}
    return {
        "dismissed": data.get("dismissed") or {},
        "bookmarked": data.get("bookmarked") or {},
    }


def apply_user_overrides(registry: dict, dismissed_urls: Iterable[str],
                         *, now: Optional[datetime] = None) -> tuple[int, int]:
    """Reconcile manually-dismissed URLs into ``permanent_rejects``.

    The set of ``permanent_rejects`` entries whose reason is
    ``user_dismissed`` is made to match ``dismissed_urls`` exactly:

      • a newly-dismissed URL is added (and dropped from ``lots`` so it
        leaves the feeds immediately and is skipped on future discovery)
      • a URL no longer in ``dismissed_urls`` (un-dismissed in the
        dashboard) has its ``user_dismissed`` reject removed, so it gets
        re-discovered and re-scraped on the next pass

    Real rejections (wrong brand, too old, …) are never touched. Returns
    ``(added, removed)`` counts."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    rejects = registry.setdefault("permanent_rejects", {})
    lots = registry.setdefault("lots", {})
    dismissed = set(dismissed_urls)

    added = 0
    for url in dismissed:
        entry = rejects.get(url)
        if not entry or entry.get("reason") != USER_DISMISSED_REASON:
            rejects[url] = {"reason": USER_DISMISSED_REASON, "rejected_at": now_iso}
            added += 1
        lots.pop(url, None)

    removed = 0
    for url in list(rejects.keys()):
        if rejects[url].get("reason") == USER_DISMISSED_REASON and url not in dismissed:
            del rejects[url]
            removed += 1

    return added, removed

