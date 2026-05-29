# Troostwijk Van Scraper — Project State

## Goal

Find underpriced cargo vans at **Troostwijk** and **Vavato** live auctions for camper-van or dual-use conversion. The pipeline discovers auction lots, classifies and scores them across two use-case tracks, and surfaces "hidden gems" (≥25 % below market, low km, good size) via Telegram alerts.

**Big van targets** (camper conversion): Peugeot Boxer / Fiat Ducato / Citroën Jumper (PSA triplets), Mercedes Sprinter, Ford Transit (L3+), Renault Master, VW Crafter, Opel Movano, MAN TGE, Iveco Daily. Sweet-spot size: **L2H2 / L3H2**.

**Small van targets** (dual-use / crew cab): VW Transporter, Renault Trafic, Opel Vivaro, Citroën Jumpy, Peugeot Expert, Ford Transit Custom. Priority: 6 legal seats, crew cab, fold-flat.

---

## Architecture — 5-stage pipeline

```
URL discovery → scraping → intelligence → cost model → registry → dashboard
```

### Stage 1 — URL discovery (`run.py`)
Collects lot URLs from:
- Troostwijk category pages: Trucks+Trailers, Vans, Cars (parent)
- Vavato category pages: same UUID paths, different host
- `IDEAL_MODELS` brand searches on both hosts (big + small van targets, 2 pages each)

Max 10 pages × 48 lots per category page. Deduplication before scraping.

### Stage 2 — Per-lot scraping (`scraper.py`)
Uses **Playwright** (chromium, headless) with `wait_until="domcontentloaded"` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`.

**Critical**: Must NOT use `wait_until="networkidle"` — Troostwijk pages continuously poll a bid GraphQL endpoint; networkidle never fires within the timeout.

Data sources per lot:
1. `__NEXT_DATA__` JSON blob (`props.pageProps.lot`) — all static fields
2. `/storefront/graphql` response (intercepted via `page.on("response")`) — live bid data

Parallelised: `crawl_parallel(urls, workers=4)` — each thread gets its own asyncio event loop + playwright instance. Progress + ETA logged every 25 lots.

### Stage 3 — Intelligence layer (`van_intel.py`)
Three sub-stages:
1. **Hard filters** — regex word-boundary checks for vehicle type, body type, damage, fuel
2. **Rule resolution** — per-model min_year/km thresholds, preferred_year for scoring
3. **Classification + dual scoring**:
   - `classify_vehicle()` → `"big"` | `"small"` | `"both"`
   - `score_big_van()` → 0-100, camper-first (usability 40%, build efficiency 25%, mechanical 20%, value 15%)
   - `score_small_van()` → 0-100, dual-use-first (utility 45%, city practicality 20%, conversion 20%, value 15%)
   - Legacy `score` (0-100) remains unchanged for backward compat and hard-filter decisions

**Model matching priority** (`_matched_model`):
1. Multi-word small van tokens first (`"transit custom"` beats `"transit"`)
2. Big van models (ALLOWED_MODELS) — beats generic body-type words like "transporter" in "Jumper Transporter"
3. Single-word small van tokens (only if no big van matched)

**Multi-signal L/H detection** (`_detect_size`):
1. Explicit `L<n>H<m>` in title
2. Standalone `\bL[1-4]\b` / `\bH[1-3]\b`
3. Roofline keywords (high roof → H2/H3, low roof → H1)
4. Length keywords (LWB/maxi → L3, SWB/kort → L1)
5. Model designation (Iveco Daily 35S/L/C suffix)
6. Weight fallback (PSA: <1900kg → L2, >2050kg → L3+)
7. Bodytype attribute fallback (box construction → H2)

### Stage 4 — Cost model (`cost_model.py`)
Computes true acquisition cost for a private (non-VAT-deductible) buyer:
```
total = hammer + buyer_premium + VAT (if non-margin-scheme) + transport + recon + fixed_fees
```
Market value priority: hammer history (≥5 samples) → multi-source median (≥3) → heuristic.

Hidden gem = deal ratio > 25%, km < 150k, year ≥ 2017, size in {L2H2, L3H2, L?H2, L2H?, L3H?}.

### Stage 5 — Registry + persistence (`registry.py`, `bid_history.py`)
`output/lot_registry.json` — priority-refresh state per URL. Tiers:
- `closing_soon` (<24h): every run
- `soon` (24-72h): every 8h
- `later` (>72h): every 22h
- `unknown`: every 12h
- `ended`: never re-scrape

`permanent_rejects` cache: URLs permanently rejected for stable reasons (brand_not_whitelisted, body_mismatch, vehicle_type, damage, size_too_small, mileage_too_high, year_below_minimum, fuel_electric) — skipped on all future discovery passes.

Cold-start cap: `MAX_NEW_PER_RUN = 400` — full catalogue registered across 3-4 runs.

---

## Key files

| File | Purpose |
|------|---------|
| `run.py` | Orchestrator — discovery → scrape → intel → cost → persist → split outputs |
| `scraper.py` | Playwright lot scraper + category/search URL crawlers |
| `van_intel.py` | Hard filters, model matching, L/H detection, big+small scoring |
| `cost_model.py` | Total acquisition cost + deal ratio + hidden gem flag |
| `registry.py` | Priority-refresh registry + permanent-reject cache |
| `bid_history.py` | Closed-auction hammer price index |
| `marktplaats.py` | Marktplaats NL price index (Adevinta JSON API) |
| `autoscout24.py` | AutoScout24 NL/DE/FR/BE price index (__NEXT_DATA__ scrape) |
| `gaspedaal.py` | Gaspedaal NL aggregator price index (schema.org JSON-LD) |
| `two_dehands.py` | 2dehands.be price index (same Adevinta API as Marktplaats) |
| `market_price.py` | Combined multi-source PriceIndex facade |
| `notify.py` | Telegram alerts for hidden gems closing within 24h |
| `fleet.py` | Fleet-type classification (utility/delivery/solar/telecom/…) |
| `models.py` | Pydantic `Vehicle` + `ScoreBreakdown` dataclasses |
| `docs/index.html` | Dashboard — all vans, links to big/small pages |
| `docs/big.html` | Big van dashboard — sorted by `big_van_score` |
| `docs/small.html` | Small van dashboard — sorted by `small_van_score` |

---

## Outputs (all in `output/`)

| File | Contents |
|------|---------|
| `latest.json` | All accepted vehicles sorted by legacy `score` |
| `latest_big_vans.json` | Big van pipeline, sorted by `big_van_score` |
| `latest_small_vans.json` | Small van pipeline, sorted by `small_van_score` |
| `rejected.json` | `{url: reason}` map for all rejected vehicles |
| `lot_registry.json` | Per-URL last-scrape state + permanent rejects |
| `bid_history.json` | Hammer history per model |
| `notified.json` | Telegram notification log |

## Logs

`logs/latest.log` — stdout from the most recent local `python3 run.py` invocation (line-buffered tee, overwritten each run). Ignored by git. Useful for debugging scrape progress, filter counts, and price index build.

---

## Platforms

**Troostwijk** (`troostwijkauctions.com`) and **Vavato** (`vavato.com`) share the same **TB-Auctions backend**. Same `__NEXT_DATA__` structure, same category UUIDs, same GraphQL endpoint. Only the hostname differs.

---

## Market price sources

| Source | Coverage | Method |
|--------|---------|--------|
| Marktplaats | NL (C2C + dealer) | Adevinta JSON API (`/lrp/api/search`) |
| AutoScout24 | NL / DE / FR / BE (dealer) | `__NEXT_DATA__` HTML scrape |
| Gaspedaal | NL aggregator | schema.org `ItemList` JSON-LD |
| 2dehands.be | BE (C2C + dealer) | Adevinta JSON API (same as Marktplaats) |

mobile.de and lacentrale.fr block headless HTTP requests (403).

---

## GH Actions workflow (`.github/workflows/scrape.yml`)

- Cron: every 6h (`0 */6 * * *`)
- Timeout: 60 min
- Commits all output files after each run; optional files (notified, big/small vans) use `|| true` so missing files don't abort the step
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (set via `gh secret set`, never `--body`)
- GitHub Pages serves `docs/` — three pages: index, big, small

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
- **"Transporter" as body-type word**: Dutch/German lot titles often say "Jumper Transporter" (= Jumper commercial vehicle). `_matched_model` checks big van models before single-word small van tokens to prevent "transporter" from overriding "jumper".
- **GraphQL parse error**: `Response.json: No resource with given identifier found` is benign — fires when response body isn't buffered yet. Wrapped in try/except.
- **cold-start run time**: First run ~15-20 min on GH Actions (400 URL cap, 4 workers). Subsequent runs 5-10 min.
- **Gaspedaal 404 on Sprinter**: `mercedes/sprinter` path returns 404 on Gaspedaal; wrapped in try/except, other models unaffected.
