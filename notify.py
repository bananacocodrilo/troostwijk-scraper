"""Telegram notifications for hidden-gem listings about to close.

Activated via ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` env vars
(set as GitHub Actions secrets). Missing env = no-op, so local runs
stay silent.

State (which lots we already pinged) lives in ``output/notified.json``
and is committed alongside the scrape outputs — that way the next run
on a fresh GH Actions VM still knows what's been alerted."""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

NOTIFY_WINDOW = timedelta(hours=24)
STATE_PATH = "output/notified.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _load_state(path: str = STATE_PATH) -> set[str]:
    try:
        with open(path) as f:
            return set(json.load(f).get("alerted", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_state(alerted: set[str], path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"alerted": sorted(alerted)}, f, indent=2)


def _parse_end(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        # Pydantic dumps as ISO with offset
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_eur(n) -> str:
    if n is None:
        return "—"
    return f"€{int(round(float(n))):,}"


def _fmt_hours_left(end: datetime) -> str:
    delta = end - datetime.now(timezone.utc)
    hrs = delta.total_seconds() / 3600
    if hrs < 1:
        return f"{int(delta.total_seconds() / 60)}min"
    return f"{hrs:.0f}h"


def _format_message(v: dict) -> str:
    end = _parse_end(v.get("auction_end"))
    left = _fmt_hours_left(end) if end else "?"
    title = v.get("title") or "(no title)"
    bid = _fmt_eur(v.get("current_bid_eur"))
    final = _fmt_eur(v.get("final_cost_estimate"))
    market = _fmt_eur(v.get("estimated_market_value"))
    ratio = v.get("deal_ratio")
    ratio_s = f"{ratio:+.0%}" if ratio is not None else "—"
    max_bid = _fmt_eur(v.get("max_recommended_bid_eur"))
    km = v.get("km")
    km_s = f"{km // 1000}k km" if km else "km ?"
    year = v.get("year") or "?"
    loc = v.get("location") or "?"
    score = v.get("score") or 0

    return (
        f"💎 Hidden gem · closes in {left}\n"
        f"{title}\n"
        f"Bid {bid} → final {final} | market {market} ({ratio_s})\n"
        f"Max recommended: {max_bid}\n"
        f"Score {score} | {year} | {km_s} | {loc}\n"
        f"{v.get('url')}"
    )


def send_telegram(text: str, *, token: Optional[str] = None,
                  chat_id: Optional[str] = None, timeout: float = 10) -> bool:
    """POST a single message. Returns True on success, False otherwise.

    Failures are swallowed (printed) so a flaky Telegram API never
    breaks the scrape pipeline."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(TELEGRAM_API.format(token=token), data=payload)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"telegram send failed: {e}")
        return False


def notify_gems(accepted: Iterable[dict], *, state_path: str = STATE_PATH,
                window: timedelta = NOTIFY_WINDOW) -> int:
    """Send a Telegram alert for every hidden-gem listing that:
      - ends within ``window`` (default 24h)
      - hasn't already been alerted

    Updates the state file. Returns the number of new alerts sent."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return 0

    alerted = _load_state(state_path)
    now = datetime.now(timezone.utc)
    sent = 0
    for v in accepted:
        if not v.get("is_hidden_gem"):
            continue
        lot_id = v.get("lot_id")
        if not lot_id or lot_id in alerted:
            continue
        end = _parse_end(v.get("auction_end"))
        if not end or end <= now:
            continue
        if end - now > window:
            continue
        if send_telegram(_format_message(v)):
            alerted.add(lot_id)
            sent += 1

    if sent:
        _save_state(alerted, state_path)
    return sent
