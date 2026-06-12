import json
import os
import re
import sys
import time as _time

import asking_feed
import bid_history
import registry
import risk
from cost_model import (
    DEFAULT_BUYER_PREMIUM,
    compute_conversion_cost,
    compute_costs,
    passes_cost_filter,
)
from market_price import build_price_index_cached
from notify import notify_gems
from scraper import VAVATO_BASE, crawl_parallel, get_category_urls, get_lot_urls
from van_intel import SCORE_THRESHOLD, WHITELIST_GROUPS, WHITELIST_TOKENS, score_small_van

MAX_BID_TARGET_FRACTION = 0.65

# Priority models for exact-name brand search against Troostwijk + Vavato.
# One per whitelist canonical name; we run a focused search for each
# because many fitting vans get mis-listed in "Cars" rather than the
# narrow "Vans" subcategory.
IDEAL_MODELS = [
    # transit_custom_l2h1 (incl. passenger Tourneo Custom)
    "Ford Transit Custom",
    "Ford Tourneo Custom",
    # expert_jumpy_proace_l2 (cargo + passenger variants share the EMP2 chassis)
    "Peugeot Expert",
    "Peugeot Traveller",
    "Citroen Jumpy",
    "Citroen SpaceTourer",
    "Toyota ProAce",
    "Toyota ProAce Verso",
    # scudo_gen3 (gen-3, 2022+; rebadged Expert/Jumpy)
    "Fiat Scudo",
    # vivaro_trafic_primastar_l2 (incl. rebadged Fiat Talento)
    "Opel Vivaro",
    "Renault Trafic",
    "Nissan Primastar",
    "Fiat Talento",
    # t6_1_lwb
    "Volkswagen Transporter",
    # psa_l1l2h1 (L1H1 and L2H1 only — confirmed low-roof short/medium)
    "Peugeot Boxer",
    "Fiat Ducato",
    "Citroen Jumper",
    # vito_v_class_l2
    "Mercedes Vito",
    "Mercedes V-Class",
    # hyundai_staria
    "Hyundai Staria",
]

# Category pages. We crawl both the narrow "Vans" subcategory (clean
# but misses lots that sellers tagged under the parent "Cars") and the
# broader parent "Cars" category (noisier but catches the strays). The
# slug pre-filter inside scraper.py drops obvious passenger-car URLs
# before they hit the per-lot scrape. Trucks-and-trailers is kept for
# heavier vans / box trucks. Vavato shares the same category UUIDs as
# Troostwijk (TB-Auctions backend), so we reuse the path and swap host.
CATEGORIES: list[tuple[str, str]] = [
    (
        "trucks-and-trailers",
        "https://www.troostwijkauctions.com/en/c/transport-logistics/trucks-trailers/fd5500c7-5590-42fb-8f0b-24fa8e6d95da",
    ),
    (
        "vans",
        "https://www.troostwijkauctions.com/en/c/transport/cars/5196727d-c14f-48dc-a2f0-e75f50094a52?categoryLevel3=b3ee855f-3320-4b3c-895c-fbf321f401d6",
    ),
    (
        "cars",
        "https://www.troostwijkauctions.com/en/c/transport/cars/5196727d-c14f-48dc-a2f0-e75f50094a52",
    ),
    (
        "vavato-vans",
        "https://www.vavato.com/en/c/transport/cars/5196727d-c14f-48dc-a2f0-e75f50094a52?categoryLevel3=b3ee855f-3320-4b3c-895c-fbf321f401d6",
    ),
    (
        "vavato-cars",
        "https://www.vavato.com/en/c/transport/cars/5196727d-c14f-48dc-a2f0-e75f50094a52",
    ),
]
# 10 × 48 = 480 listings per category; well above current vans count (274)
# and stops early on the first empty page anyway.
CATEGORY_PAGES = 10
BRAND_PAGES = 2       # Troostwijk brand searches
VAVATO_BRAND_PAGES = 1  # Vavato page 2 consistently times out (30s wasted per search)


def _model_key(title: str) -> str:
    """Return the lower-cased whitelist token present in ``title`` (used to key
    bid history / price index lookups). Multi-word tokens checked first."""
    s = (title or "").lower()
    # Iterate longest-first so "transit custom" beats "transporter" etc.
    for token in sorted(WHITELIST_TOKENS, key=len, reverse=True):
        if " " in token or "." in token:
            parts = re.split(r"[\s.]+", token)
            pat = r"\b" + r"\s*\.?\s*".join(re.escape(p) for p in parts) + r"\b"
            if re.search(pat, s):
                return token
        elif re.search(rf"\b{re.escape(token)}\b", s):
            return token
    return ""


# Stable output schema for latest.json.
# Every field is always present (None if unavailable). Order is fixed.
_SCHEMA: dict = {
    # Identity
    "url":                        None,
    "lot_id":                     None,
    "title":                      None,
    "thumbnail_url":              None,
    "images":                     [],
    "source":                     None,
    "platform":                   None,
    # Vehicle
    "year":                       None,
    "km":                         None,
    "fuel":                       None,
    "emission_standard":          None,
    "van_type":                   None,
    "model_group":                None,
    "variant":                    None,
    "classification_confidence":  None,
    "seats":                      None,
    "body_type":                  None,
    "weight_kg":                  None,
    "load_kg":                    None,
    "city":                       None,
    "country_code":               None,
    # Auction
    "current_bid_eur":            None,
    "buyer_premium_pct":          None,
    "bids_count":                 None,
    "auction_end":                None,
    "bidding_status":             None,
    "condition":                  None,
    "vat_margin":                 None,
    # Market & cost
    "estimated_market_value":     None,
    "market_value_source":        None,
    "max_recommended_bid_eur":    None,
    "final_cost_estimate":        None,
    "transport_cost_estimate":    None,
    "reconditioning_cost_estimate": None,
    "deal_ratio":                 None,
    # Conversion
    "est_conversion_cost_eur":      None,
    "est_conversion_cost_low_eur":  None,
    "est_conversion_cost_high_eur": None,
    "conversion_effort":            None,
    "total_project_cost_eur":       None,
    # Scores
    "score":                      None,
    "risk_score":                 0,
    "risk_flags":                 [],
    "is_hidden_gem":              False,
}


def _normalize(v: dict) -> dict:
    """Return a stable-schema dict for output. Always the same fields, same order."""
    return {field: v.get(field, default) for field, default in _SCHEMA.items()}


def _dump(path: str, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def _dump_vans(path: str, vans: list[dict]):
    _dump(path, [_normalize(v) for v in vans])


def main():
    # Time budget — gives us a clean exit before GH Actions kills the job.
    # Scrape job timeout is 50 min; we stop accepting new lots at 40 min
    # and skip market refresh at 46 min, leaving ≥4 min for output + commit.
    _start = _time.monotonic()
    _SCRAPE_DEADLINE  = 40 * 60   # stop URL scraping after this many seconds
    _MARKET_DEADLINE  = 46 * 60   # skip market price refresh after this

    def _elapsed() -> float:
        return _time.monotonic() - _start

    def _fmt(s: float) -> str:
        return f"{s/60:.1f}min"
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

    # Brand-keyword searches were previously added to catch lots that
    # didn't appear in the category pages. In practice they returned
    # mostly noise (passenger cars, unrelated items matching the brand
    # name) so we now rely on the category pages alone. QUERIES is still
    # used for the Marktplaats price index below.
    #
    # ...except for the IDEAL_MODELS — the conversion-sweet-spot vans
    # we explicitly care about. We run a targeted brand-name search for
    # each, on both Troostwijk and Vavato, because sellers frequently
    # mis-tag them under "Cars" (already crawled) but also surface them
    # via search results that the category page misses.
    print("Collecting URLs from ideal-model brand searches:")
    for query in IDEAL_MODELS:
        try:
            _add(f"twk:{query}", get_lot_urls(query, pages=BRAND_PAGES))
        except Exception as e:
            print(f"  twk:{query} failed: {e}")
        try:
            _add(f"vavato:{query}", get_lot_urls(query, pages=VAVATO_BRAND_PAGES, base=VAVATO_BASE))
        except Exception as e:
            print(f"  vavato:{query} failed: {e}")

    print(f"\nTotal unique URLs discovered: {len(all_urls)}")

    # 2. Discovery + priority refresh. The registry holds last-known
    #    state per URL; we only re-scrape URLs that are new or whose
    #    priority tier says they're stale (closing-soon every run,
    #    24-72h every other run, >72h daily). Stale-but-known lots keep
    #    their previous data so latest.json reflects the full universe.
    reg = registry.load()
    pruned = registry.prune(reg)
    if pruned:
        print(f"  registry: pruned {pruned} stale entries (auction ended > {registry.PRUNE_DAYS_AFTER_END}d ago)")

    urls_to_scrape, refresh_stats = registry.select_urls_to_scrape(all_urls, reg)
    print(f"  registry: refreshing {len(urls_to_scrape)}/{len(all_urls)} URLs — {refresh_stats}")

    # 3. Scrape only the selected URLs, in batches of 60 so we can check
    #    elapsed time between batches and stop gracefully before GH Actions
    #    kills the job. Each batch takes ~2-4 min with 4 Playwright workers.
    #    Unscraped URLs stay in the registry and get picked up next run.
    _BATCH = 60
    fresh_results: list[dict] = []
    total_batches = (len(urls_to_scrape) + _BATCH - 1) // _BATCH
    for _bi, _start_i in enumerate(range(0, len(urls_to_scrape), _BATCH)):
        if _elapsed() > _SCRAPE_DEADLINE:
            _skipped = len(urls_to_scrape) - _start_i
            print(
                f"  ⏰ soft deadline at {_fmt(_elapsed())} — stopping after "
                f"batch {_bi}/{total_batches}, skipping {_skipped} URLs "
                f"(will retry next run)"
            )
            break
        _batch = urls_to_scrape[_start_i:_start_i + _BATCH]
        print(f"  scrape batch {_bi+1}/{total_batches} "
              f"({len(_batch)} URLs, elapsed {_fmt(_elapsed())})")
        fresh_results.extend(crawl_parallel(_batch, workers=4))

    # 4. Merge fresh data into registry and persist.
    registry.merge(reg, fresh_results)
    registry.save(reg)

    # all_results = full union (fresh + stale-but-known). Everything
    # downstream operates on this so notifications/dashboard reflect
    # every lot we've ever discovered that's still active.
    all_results = registry.all_known_vehicles(reg)

    # Deduplicate by lot-ID suffix (A3-44485-10662) — the same physical auction
    # lot is often listed under both troostwijkauctions.com and vavato.com URLs.
    # Keep the Troostwijk URL when both exist (it's the primary platform).
    _seen_lot_ids: dict = {}
    _deduped: list = []
    for v in all_results:
        m = re.search(r"(A\d+-\d+-\d+)", v.get("url", ""))
        lot_id = m.group(1) if m else None
        if lot_id is None:
            _deduped.append(v)
            continue
        existing = _seen_lot_ids.get(lot_id)
        if existing is None:
            _seen_lot_ids[lot_id] = v
            _deduped.append(v)
        elif "troostwijkauctions.com" in v.get("url", ""):
            # Prefer TWK URL — replace in-place
            _deduped[_deduped.index(existing)] = v
            _seen_lot_ids[lot_id] = v
    all_results = _deduped

    # 4a. Persist a bid-history snapshot of every freshly-scraped lot.
    #     We use fresh_results (not all_results) so we don't re-record
    #     duplicate snapshots for lots we didn't actually re-fetch.
    bid_history.update(fresh_results, model_token_of=_model_key)
    hammer_index = bid_history.load_index()

    # 4b. Combined market price index (Marktplaats + AutoScout24).
    # Skip the HTTP refresh if we're running low on time — use the cached
    # data from the previous run instead. The index will be slightly stale
    # but still good enough for deal-ratio math; the next run will refresh.
    _do_refresh = _elapsed() < _MARKET_DEADLINE
    if not _do_refresh:
        print(f"  ⏰ skipping market refresh at {_fmt(_elapsed())} — using cached prices")
    price_index = build_price_index_cached(refresh=_do_refresh)

    # 5. Attach market data + compute true cost + re-filter
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

        # True cost model (computes estimated_market_value via all sources + heuristic)
        cost_fields = compute_costs(v, model_token=mk)
        v.update(cost_fields)

        # Max recommended bid — use the best market value we have (may be
        # heuristic when year is missing or market data is sparse).
        est_market = v.get("estimated_market_value")
        if est_market:
            premium = 1 + (v.get("buyer_premium_pct") or DEFAULT_BUYER_PREMIUM * 100) / 100
            v["max_recommended_bid_eur"] = round(est_market * MAX_BID_TARGET_FRACTION / premium)

        # Conversion-cost estimate + total project cost — uses model_group
        # + passenger/crew-cab/kombi signals to band the conversion budget,
        # then adds it to acquisition for the headline "what will this
        # really cost me" number.
        v.update(compute_conversion_cost(v))

        # Cost filter (overpaying vs market, or too expensive to recondition)
        passes, cost_reason = passes_cost_filter(v)
        if not passes:
            v["rejected_reason"] = cost_reason
            cost_rejected.append(v)
            continue

        # cost_model adds deal_score separately — do NOT overwrite score.
        # score = suitability (always matches ScoreBreakdown fields).
        # deal_score = financial quality (deal_ratio-derived, shown separately).

        # Risk metadata — purely additive (no impact on accept/reject).
        v.update(risk.compute_risk(v))

        accepted.append(v)

    rejected = suitability_rejected + cost_rejected
    accepted.sort(key=lambda v: v.get("score") or 0, reverse=True)

    _dump_vans("output/latest.json", accepted)
    # High-roof feed: all groups except transit_custom_l2h1 (H1-only).
    l2h2 = [v for v in accepted if v.get("model_group") != "transit_custom_l2h1"]
    _dump_vans("output/l2h2.json", l2h2)
    _dump("output/rejected.json", {
        v["url"]: v.get("rejected_reason") or "unknown"
        for v in rejected if v.get("url")
    })

    reason_counts: dict = {}
    for v in rejected:
        r = (v.get("rejected_reason") or "unknown").split(":", 1)[0]
        reason_counts[r] = reason_counts.get(r, 0) + 1

    # Per-whitelist-group breakdown of accepted candidates
    group_counts: dict = {gk: 0 for gk in WHITELIST_GROUPS}
    for v in accepted:
        gk = v.get("model_group")
        if gk in group_counts:
            group_counts[gk] += 1

    load_failures = reason_counts.pop("load_failed", 0)
    gems = [v for v in accepted if v.get("is_hidden_gem")]
    print(
        f"\ndiscovered={len(all_urls)} scraped_this_run={len(fresh_results)} "
        f"known_total={len(all_results)} accepted={len(accepted)} "
        f"load_failed={load_failures} filtered={len(rejected) - load_failures}"
    )
    print("camper-candidate groups (accepted):")
    for gk in WHITELIST_GROUPS:
        print(f"  {gk}: {group_counts[gk]}")
    for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  reject:{r}: {n}")

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

    # ── Asking-price feed ────────────────────────────────────────────────
    # Parallel pipeline: reuses the price_cache.json we just refreshed and
    # writes a deduplicated cross-source feed of whitelisted asking-price
    # listings. No new HTTP — runs in seconds.
    print("\nbuilding asking-price feed...")
    asking_count = asking_feed.write_feed()
    print(f"  wrote {asking_count} listings to output/asking_listings.json")

    print(f"\n⏱️  total run time: {_fmt(_elapsed())}")


if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    log_file = open("logs/latest.log", "w", buffering=1)  # line-buffered
    # Tee stdout to both terminal and log file
    class _Tee:
        def __init__(self, *streams): self._s = streams
        def write(self, d):
            for s in self._s: s.write(d)
        def flush(self):
            for s in self._s: s.flush()
    sys.stdout = _Tee(sys.__stdout__, log_file)
    try:
        main()
    finally:
        sys.stdout = sys.__stdout__
        log_file.close()
