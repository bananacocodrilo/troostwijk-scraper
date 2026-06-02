# Troostwijk Van Scraper — Project State

## Goal

Find underpriced **camper-candidate small/mid vans** at **Troostwijk** and **Vavato** live auctions, filtered to a strict whitelist of 8 model groups (mostly in the Transit Custom L2H1 dimensional class — ~5.3m × 2.0m — Euro 6, 6-seat compatible). The pipeline discovers auction lots, classifies them against the whitelist, applies a strict-but-soft-gated filter (only confirmed violations reject), scores survivors for camper-conversion suitability and rental ROI, and surfaces hidden gems via Telegram alerts.

A second, complementary feed (`asking_feed.py` → `docs/asking.html`) reuses the same whitelist + classifier to surface **fixed-price marketplace listings** (Marktplaats, AutoScout24 NL/DE, 2dehands.be) aggregated and deduped across sources. Deal-ratio scoring doesn't apply (no hammer/auction), so listings are ranked by price percentile vs the cohort median of the same (model_group, year ±2). The two feeds run in the same `python3 run.py` invocation but produce independent outputs.

**Whitelist groups (the only models that pass the classifier):**

| Group key | Models | Required size | min year | Notes |
|-----------|--------|---------------|----------|-------|
| `transit_custom_l2h1` | Ford Transit Custom + Ford Tourneo Custom (passenger) | L2H1 | 2016 | H1 is the only height variant |
| `expert_jumpy_proace_l2` | Peugeot Expert / Citroën Jumpy / Toyota ProAce | L2, any H | 2016 | EMP2 platform gen-3 |
| `scudo_gen3` | Fiat Scudo (gen 3, 2022+) | L2, any H | 2022 | Rebadged Expert/Jumpy; separate group to exclude old Scudo (2007-2016) |
| `vivaro_trafic_primastar_l2` | Opel Vivaro / Renault Trafic / Nissan Primastar / Fiat Talento | L2, any H | 2015 | shared NV300 platform; Talento is rebadged Trafic |
| `t6_1_lwb` | VW Transporter T6.1 | L2 (LWB), any H | 2020 | T6.1 facelift = Euro 6d |
| `psa_l1l2h1` | Peugeot Boxer / Fiat Ducato / Citroën Jumper | L1 or L2, H1 only | 2016 | Low-roof short/medium Sevel-platform vans; H2/H3/L3/L4 reject |
| `vito_v_class_l2` | Mercedes Vito / V-Class (Lang or Extralang) | L2 or L3, any H | 2015 | W447 chassis; Kompakt variant rejects via L1 keyword |
| `hyundai_staria` | Hyundai Staria | any | 2021 | Korean MPV, single length (5253mm) |

All other van families (Sprinter, Crafter, TGE, Master, Movano, Daily, plain Transit, etc.) are rejected at the classifier stage as `brand_not_in_whitelist`.

**Soft-gate policy** — only *confirmed* violations reject:
- Year known and `< min_year` for group → reject
- Emission known and below Euro 6 → reject
- Seats known and `< 5` → reject (5-seat Double Cab = valid camper candidate)
- Size known and outside the group's allowed L/H → reject
- Any of these unknown → PASS

Exception: model classification is a hard gate. No whitelist token match → reject.

---

## Architecture — 5-stage pipeline

```
URL discovery → scraping → intelligence → cost model → registry → dashboard
```

### Stage 1 — URL discovery (`run.py`)
Collects lot URLs from:
- Troostwijk category pages: Trucks+Trailers, Vans, Cars (parent)
- Vavato category pages: same UUID paths, different host
- `IDEAL_MODELS` brand searches on both hosts (one per whitelist canonical name, 2 pages each)

Max 10 pages × 48 lots per category page. Deduplication before scraping.

### Stage 2 — Per-lot scraping (`scraper.py`)
Uses **Playwright** (chromium, headless) with `wait_until="domcontentloaded"` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`.

**Critical**: Must NOT use `wait_until="networkidle"` — Troostwijk pages continuously poll a bid GraphQL endpoint; networkidle never fires within the timeout.

Data sources per lot:
1. `__NEXT_DATA__` JSON blob (`props.pageProps.lot`) — all static fields
2. `/storefront/graphql` response (intercepted via `page.on("response")`) — live bid data

Parallelised: `crawl_parallel(urls, workers=4)` — each thread gets its own asyncio event loop + playwright instance.

### Stage 3 — Intelligence layer (`van_intel.py`)

Per-spec flow:
```
raw_listing
  → hard filters       (vehicle type / body / damage / fuel / mileage)
  → classify_vehicle   (must match one of 4 whitelist groups)
  → strict_filter      (size / year / Euro / seats — soft gate on unknowns)
  → score_small_van    (camper-candidate suitability, 0-100)
  → score_roi          (rental-income ranking, 0-10 + S/A/B/C tier)
```

**Key functions:**
- `classify_vehicle(title, description, *, weight_kg=None, body_type=None) -> Classification` — returns `(group, variant, confidence, matched_token, evidence)`. `group=None` means no whitelist match → caller must reject.
- `strict_filter(vehicle, classification) -> (passed, reason)` — applies the camper-candidate hard gate (soft on unknowns).
- `evaluate(vehicle) -> Evaluation` — composed entry point; calls hard filters → classify → strict_filter → score.

**Multi-signal L/H detection** (`_detect_size`):
1. Explicit `L<n>H<m>` in title
2. Standalone `\bL[1-4]\b` / `\bH[1-3]\b`
3. Roofline keywords (high roof → H2/H3, low roof → H1)
4. Length keywords (LWB/Maxi → L3 generally, but **clamped to L2** for whitelist groups whose `required_length=[2]` only, since those families have no L3 variants in their factory nomenclature; the `vito_v_class_l2` group accepts L3 since Mercedes Extralang is genuinely L3-class)
5. Weight fallback (per-model bands tuned for the small-van families)
6. Bodytype attribute fallback (box construction → H2)

**Model matching priority** (`_match_whitelist_token`):
1. Multi-word tokens first (`"transit custom"` beats `"transit"`, which is NOT in the whitelist)
2. Dotted tokens (`"t6.1"`) before bare equivalents
3. Single-word tokens last

`SMALLER_SIBLINGS` (Vito, Caddy, Transit Connect, Kangoo, Combo, Berlingo, Partner, etc.) reject before any whitelist match is attempted — protects against e.g. "Transit Connect" matching the "transit" token (which isn't even in the whitelist anyway).

### Stage 4 — Cost model (`cost_model.py`)
Computes true acquisition cost for a private (non-VAT-deductible) buyer:
```
total = hammer + buyer_premium + VAT (if non-margin-scheme) + transport + recon + fixed_fees
```
Market value priority: hammer history (≥5 samples) → multi-source median (≥3) → heuristic.

`_BASE_PRICES["small_van"]` is the heuristic value table for the whitelist groups. Legacy big-van groups (psa/premium/mid) are retained harmlessly for cached pre-pivot entries.

Hidden gem = deal ratio > 25%, km < 150k, year ≥ 2017, size in `{L2H1, L2H2, L2H?, L2}`.

### Stage 5 — Registry + persistence (`registry.py`, `bid_history.py`)
`output/lot_registry.json` — priority-refresh state per URL. Tiers:
- `closing_soon` (<24h): every run
- `soon` (24-72h): every 8h
- `later` (>72h): every 22h
- `unknown`: every 12h
- `ended`: never re-scrape

`permanent_rejects` cache: URLs permanently rejected for stable reasons (`brand_not_in_whitelist`, `body_mismatch`, `vehicle_type`, `damage`, `size_not_allowed`, `mileage_too_high`, `year_below_minimum`, `emission_below_euro6`, `seats_below_5`, `smaller_sibling`) — skipped on all future discovery passes. **Note:** electric vehicles are NOT rejected — eVito / eTransporter / e-Expert and similar are valid camper-conversion candidates.

Cold-start cap: `MAX_NEW_PER_RUN = 400` — full catalogue registered across 3-4 runs.

---

## Key files

| File | Purpose |
|------|---------|
| `run.py` | Orchestrator — discovery → scrape → intel → cost → persist → output |
| `scraper.py` | Playwright lot scraper + category/search URL crawlers |
| `van_intel.py` | Hard filters, whitelist classifier, strict filter, small-van + ROI scoring |
| `cost_model.py` | Total acquisition cost + deal ratio + hidden gem flag |
| `registry.py` | Priority-refresh registry + permanent-reject cache |
| `bid_history.py` | Closed-auction hammer price index |
| `marktplaats.py` | Marktplaats NL price index (Adevinta JSON API) |
| `autoscout24.py` | AutoScout24 NL/DE/FR/BE price index (__NEXT_DATA__ scrape) |
| `gaspedaal.py` | Gaspedaal NL aggregator price index (schema.org JSON-LD) |
| `two_dehands.py` | 2dehands.be price index (same Adevinta API as Marktplaats) |
| `autotrack.py` | AutoTrack NL price index (RSC-chunk extraction from Next.js SPA) |
| `market_price.py` | Combined multi-source PriceIndex facade |
| `asking_feed.py` | Asking-price aggregator — reuses `price_cache.json` to emit a deduped cross-source feed of whitelist-matching listings |
| `notify.py` | Telegram alerts for hidden gems closing within 24h |
| `fleet.py` | Fleet-type classification (utility/delivery/solar/telecom/…) |
| `models.py` | Pydantic `Vehicle` dataclass (incl. `model_group`, `variant`, `classification_confidence`) |
| `docs/index.html` | Camper-candidate dashboard (auction feed) |
| `docs/asking.html` | Asking-price aggregator dashboard (Marktplaats / AutoScout24 / 2dehands / Autotrack) |

---

## Outputs (all in `output/`)

| File | Contents |
|------|---------|
| `latest.json` | Accepted camper candidates from the auction feed, sorted by `score` (small-van suitability) |
| `asking_listings.json` | Deduped cross-source asking-price feed (Marktplaats / AutoScout24 / 2dehands / Autotrack), filtered through the same whitelist as the auction feed. Sorted underpriced-first. |
| `rejected.json` | `{url: reason}` map for all rejected vehicles |
| `lot_registry.json` | Per-URL last-scrape state + permanent rejects |
| `bid_history.json` | Hammer history per model token |
| `price_cache.json` | Per-source listings cache populated by `market_price.build_price_index_cached`; read by `asking_feed.py` |
| `notified.json` | Telegram notification log |

## Logs

`logs/latest.log` — stdout from the most recent local `python3 run.py` invocation (line-buffered tee, overwritten each run). Ignored by git.

---

## Platforms

**Troostwijk** (`troostwijkauctions.com`) and **Vavato** (`vavato.com`) share the same **TB-Auctions backend**. Same `__NEXT_DATA__` structure, same category UUIDs, same GraphQL endpoint. Only the hostname differs.

---

## Market price sources

| Source | Coverage | Method |
|--------|---------|--------|
| Marktplaats | NL (C2C + dealer) | Adevinta JSON API (`/lrp/api/search`) |
| AutoScout24 | NL / DE / FR / BE (dealer) | `__NEXT_DATA__` HTML scrape |
| AutoTrack | NL (dealer) | Next.js RSC chunk extraction (`self.__next_f.push`) |
| Gaspedaal | NL aggregator | schema.org `ItemList` JSON-LD |
| 2dehands.be | BE (C2C + dealer) | Adevinta JSON API (same as Marktplaats) |

mobile.de and lacentrale.fr block headless HTTP requests (403).

---

## GH Actions workflow (`.github/workflows/scrape.yml`)

- Cron: every 6h (`0 */6 * * *`)
- Timeout: 60 min
- Commits all output files after each run
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- GitHub Pages serves `docs/` — `index.html` (camper candidates) + `roi.html`

---

## Running locally

```bash
python3 run.py
```

Logs go to both stdout and `logs/latest.log`. Uses `output/lot_registry.json` if present (cold start if missing = cap 400 new URLs).

---

## Known quirks

- **networkidle timeout**: Must use `domcontentloaded` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`. Never switch back to networkidle.
- **Word-boundary filters**: All keyword lists in `van_intel.py` use `\b` regex boundaries to avoid "bus" matching "business", "partner" matching "business partner", etc.
- **engine failure false positive**: Pattern uses negative lookbehind for "no/geen/kein" and excludes "engine failure codes" (OBD context).
- **LWB → L2 clamp**: "LWB" / "Maxi" / "lang" map to L3 globally (correct for big vans) but are clamped down to L2 inside `classify_vehicle` for whitelist groups whose `required_length=[2]` only, since those families have no L3 variants. The `vito_v_class_l2` group (Mercedes Extralang really IS L3-class) is exempt. Explicit "L3" / "L4" markers still reject for L2-only groups.
- **T6.1 detection**: permissive — any "Transporter" match enters the `t6_1_lwb` group; soft-gates on year (rejects only if `year < 2020` confirmed). Lots without an explicit year pass through.
- **GraphQL parse error**: `Response.json: No resource with given identifier found` is benign — fires when response body isn't buffered yet. Wrapped in try/except.
- **cold-start run time**: First run ~15-20 min on GH Actions (400 URL cap, 4 workers). Subsequent runs 5-10 min.
- **Backward-compat fields**: `models.py` keeps `van_category`, `big_van_score`, `small_van_score` as deprecated unused fields so old `lot_registry.json` snapshots still deserialise.
