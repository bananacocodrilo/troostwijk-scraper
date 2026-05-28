"""Bid-history tracker.

Accumulates per-run snapshots of current bid / bids_count / status for
every lot we scrape. Once a lot's ``auction_end`` is in the past, we
freeze its last known bid as ``final_hammer_eur`` and treat that as the
ground-truth close price.

The resulting ``HammerIndex`` is queried by ``cost_model.compute_costs``
as a higher-priority market reference than Marktplaats — but only once
we've accumulated enough closed auctions for that model/year bucket
(``MIN_SAMPLES``). Below the threshold we fall back to Marktplaats so
sparse buckets don't poison the estimate.

State file: ``output/bid_history.json``, committed alongside the other
outputs so the dataset accumulates across GH Actions runs."""

import json
import os
import statistics
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

PATH = "output/bid_history.json"

# Years per bucket on either side of the target year (matches Marktplaats
# bucketing, so the two sources are directly comparable).
YEAR_WINDOW = 2

# Minimum closed-auction samples in a (model_token, year ± YEAR_WINDOW)
# bucket before we trust the hammer median over Marktplaats. Tuned
# conservatively — 5 closed auctions across a 5-year band is roughly
# 2-3 weeks of data at the current scrape cadence.
MIN_SAMPLES = 5


def _load(path: str = PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lots": {}}


def _save(data: dict, path: str = PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _parse_end(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def update(vehicles: Iterable[dict],
           model_token_of: Callable[[str], Optional[str]],
           *, path: str = PATH) -> dict:
    """Snapshot every vehicle with a lot_id. Finalises auctions whose end
    is in the past. Returns the updated state dict."""
    data = _load(path)
    lots = data.setdefault("lots", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    now_dt = datetime.now(timezone.utc)

    for v in vehicles:
        lot_id = v.get("lot_id")
        if not lot_id:
            continue
        mk = model_token_of(v.get("title", "") or "")
        # We only want hammer data for lots we actually classify as vans
        # — non-van lots (passenger cars, semi-trailers) have different
        # price dynamics and would poison the index.
        if not mk:
            continue

        entry = lots.setdefault(lot_id, {
            "url": v.get("url"),
            "model_token": mk,
            "year": v.get("year"),
            "snapshots": [],
            "final_hammer_eur": None,
            "closed_at": None,
        })
        # Backfill in case earlier snapshot didn't have year yet.
        entry["model_token"] = entry.get("model_token") or mk
        if not entry.get("year") and v.get("year"):
            entry["year"] = v["year"]

        # Skip duplicate snapshots (same bid + count + status as last one).
        snap = {
            "ts": now_iso,
            "current_bid_eur": v.get("current_bid_eur"),
            "bids_count": v.get("bids_count"),
            "status": v.get("bidding_status"),
        }
        last = entry["snapshots"][-1] if entry["snapshots"] else None
        if last is None or any(snap[k] != last.get(k) for k in ("current_bid_eur", "bids_count", "status")):
            entry["snapshots"].append(snap)

        # Finalise once the auction end is behind us.
        if entry.get("final_hammer_eur") is None:
            end = _parse_end(v.get("auction_end"))
            last_bid = v.get("current_bid_eur")
            if end and end < now_dt and last_bid and last_bid > 0:
                entry["final_hammer_eur"] = float(last_bid)
                entry["closed_at"] = v.get("auction_end")

    _save(data, path)
    return data


class HammerIndex:
    """(model_token, year) → list of final hammer EUR. Median lookup
    pools across ``±YEAR_WINDOW`` years; returns None below
    ``MIN_SAMPLES``."""

    def __init__(self, data: dict):
        self._buckets: dict = {}
        for entry in (data.get("lots") or {}).values():
            hammer = entry.get("final_hammer_eur")
            mk = entry.get("model_token")
            yr = entry.get("year")
            if hammer and mk and yr:
                self._buckets.setdefault((mk, yr), []).append(float(hammer))

    def _pool(self, model_token: Optional[str], year: Optional[int]) -> list:
        if not model_token or not year:
            return []
        pool: list = []
        for dy in range(-YEAR_WINDOW, YEAR_WINDOW + 1):
            pool.extend(self._buckets.get((model_token, year + dy), []))
        return pool

    def median(self, model_token: Optional[str], year: Optional[int]) -> Optional[float]:
        pool = self._pool(model_token, year)
        if len(pool) < MIN_SAMPLES:
            return None
        return round(statistics.median(pool))

    def sample_size(self, model_token: Optional[str], year: Optional[int]) -> int:
        return len(self._pool(model_token, year))


def load_index(path: str = PATH) -> HammerIndex:
    return HammerIndex(_load(path))
