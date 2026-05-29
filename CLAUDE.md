# Troostwijk Van Scraper — Project State

## Goal

Find underpriced large cargo vans at **Troostwijk** and **Vavato** live auctions for cheap camper-van conversion. The pipeline discovers auction lots, scores them by suitability and deal quality, and surfaces "hidden gems" (≥25 % below market, low km, good size) with Telegram alerts.

Target van platform: Peugeot Boxer / Fiat Ducato / Citroën Jumper (PSA triplets), Mercedes Sprinter, Ford Transit, Renault Master, VW Crafter, Opel Movano, MAN TGE, Iveco Daily, VW Transporter.

Sweet-spot size: **L2H2 / L3H2** (stand-up roof, manageable length). L4 and H3 are explicitly down-scored — too big for parking/camping use.

---

## Architecture — 5-stage pipeline

```
URL discovery → scraping → intelligence → cost model → registry → dashboard
```

### Stage 1 — URL discovery (`run.py`)
Collects lot URLs from:
- Troostwijk category pages: Trucks+Trailers, Vans, Cars (parent)
- Vavato category pages: same UUID paths, different host
- IDEAL_MODELS brand searches on both hosts (Boxer/Jumper/Ducato/Sprinter, 2 pages each)

Max 10 pages × 48 lots per category page. Deduplication before hitting the per-lot scrape.

### Stage 2 — Per-lot scraping (`scraper.py`)
Uses **Playwright** (chromium, headless) with `wait_until="domcontentloaded"` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`.

**Critical**: Must NOT use `wait_until="networkidle"` — Troostwijk pages continuously poll a bid GraphQL endpoint; networkidle never fires within the timeout.

Data comes from two sources per lot:
1. `__NEXT_DATA__` JSON blob (`props.pageProps.lot`) — all static fields (title, year, km, condition, images, attributes)
2. `/storefront/graphql` response (intercepted via `page.on("response")`) — live bid data (current_bid_eur, buyer_premium_pct, total_cost_eur, bids_count)

Parallelised: `crawl_parallel(urls, workers=4)` — each thread gets its own asyncio event loop + playwright instance.

Progress + ETA logged every 25 lots.

### Stage 3 — Intelligence layer (`van_intel.py`)
Three sub-stages:
1. **Hard filters** — regex word-boundary checks for vehicle type, body type, damage, fuel. Rejection reasons prefixed with permanent-reject strings get cached.
2. **Rule resolution** — per-model min_year/km thresholds, preferred_year for scoring.
3. **Scoring** — 0-100. Key sub-scores:
   - `year` (0-30): newer = better
   - `mileage` (0-25): lower = better, per-model curves
   - `van_size` (0-20): L2H2/L3H2 = 18-20; L4/H3 max 8; H1 = 0; unknown = 5
   - `emission` (0-10): Euro 6 preferred
   - `resaleability` (0-10): premium brands score higher
   - `crew_cab` (0-5): crew cab bonus

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
Market value priority: hammer history (≥5 samples) → Marktplaats median (≥3) → heuristic.

Deal ratio = (market_value - total_cost) / total_cost. Hidden gem = ratio > 25%, km < 150k, year ≥ 2017, size in {L2H2, L3H2, L?H2, L2H?, L3H?}.

### Stage 5 — Registry + persistence (`registry.py`, `bid_history.py`)
`output/lot_registry.json` — priority-refresh state per URL. Tiers:
- `closing_soon` (<24h): every run
- `soon` (24-72h): every 8h  
- `later` (>72h): every 22h
- `unknown`: every 12h
- `ended`: never re-scrape

`permanent_rejects` cache: URLs that failed with stable reasons (brand_not_whitelisted, body_mismatch, vehicle_type, damage, size_too_small, mileage_too_high, year_below_minimum, fuel_electric) are stored here and skipped on every future discovery pass.

Cold-start cap: `MAX_NEW_PER_RUN = 400` — new URLs are randomly sampled; full catalogue registered across 3-4 runs.

`output/bid_history.json` — closed-auction hammer prices per (model_token, year). Used as preferred market reference once ≥5 samples exist.

`output/notified.json` — Telegram notification state to avoid duplicate alerts.

---

## Key files

| File | Purpose |
|------|---------|
| `run.py` | Orchestrator — discovery → scrape → intel → cost → persist |
| `scraper.py` | Playwright lot scraper + category/search URL crawlers |
| `van_intel.py` | Hard filters, scoring, L/H size detection |
| `cost_model.py` | Total acquisition cost + deal ratio + hidden gem flag |
| `registry.py` | Priority-refresh registry + permanent-reject cache |
| `bid_history.py` | Closed-auction hammer price index |
| `marktplaats.py` | Marktplaats retail price index (HTTP JSON API) |
| `autoscout24.py` | AutoScout24 retail price index (HTML scrape) |
| `market_price.py` | Combined multi-source PriceIndex facade |
| `notify.py` | Telegram alerts for hidden gems closing within 24h |
| `fleet.py` | Fleet-type classification (utility/delivery/solar/telecom/…) |
| `models.py` | Pydantic `Vehicle` + `ScoreBreakdown` dataclasses |
| `docs/index.html` | GitHub Pages dashboard (vanilla JS, no build step) |

---

## Outputs (all in `output/`)

| File | Contents |
|------|---------|
| `latest.json` | All accepted vehicles (passed hard filters + cost filter), sorted by score |
| `rejected.json` | All rejected vehicles with reason |
| `lot_registry.json` | Per-URL last-scrape state + permanent rejects |
| `bid_history.json` | Hammer history per model |
| `notified.json` | Telegram notification log |

---

## Platforms

**Troostwijk** (`troostwijkauctions.com`) and **Vavato** (`vavato.com`) share the same **TB-Auctions backend**. Same `__NEXT_DATA__` structure, same category UUIDs, same GraphQL endpoint at `/storefront/graphql`. Only the hostname differs.

---

## GH Actions workflow (`.github/workflows/scrape.yml`)

- Cron: every 6h (`0 */6 * * *`)
- Timeout: 60 min
- Commits `output/latest.json`, `output/rejected.json`, `lot_registry.json`, `bid_history.json`, `notified.json` after each run
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (set via `gh secret set`)
- GitHub Pages serves `docs/index.html` reading `../output/latest.json`

---

## Running locally

```bash
python3 run.py
```

No arguments. Uses `output/lot_registry.json` if present (cold start if missing = cap 400 new URLs this run).

---

## Known quirks

- **networkidle timeout**: Must use `domcontentloaded` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`. Never switch back to networkidle.
- **Word-boundary filters**: All keyword lists in `van_intel.py` use `\b` regex boundaries to avoid "bus" matching "business", "partner" matching "business partner", etc.
- **GraphQL parse error**: `Response.json: No resource with given identifier found` is benign — it fires when the response is streamed before the body is fully buffered. Wrapped in try/except, doesn't affect lot data.
- **Vavato brand searches too slow**: Replaced with single category page URL. Brand searches on Vavato return too many irrelevant results and add ~300 extra lots.
- **cold-start run time**: First run (empty registry) typically takes 15-20 min on GH Actions with 400 URL cap and 4 workers. Subsequent runs are 5-10 min.
- **Token security**: NEVER pass `--body` to `gh secret set`. Run interactively: `gh secret set TELEGRAM_BOT_TOKEN` (prompts for value, doesn't log to shell history).

---

## Pending / in-progress

- AutoScout24 as second market price source (in progress)
- UI: small vs big van split with toggle button (not started)
- Bid history hammer index growing — will supersede Marktplaats once ≥5 samples per model/year
