# Troostwijk Van Scraper — Project State

## Goal

> **June 2026 pivot — L2H2 high-roof, German-first, asking-feed primary.** Three learnings reshaped the project: (1) **auctions aren't worth it** — the Troostwijk/Vavato pipeline is kept but is no longer the focus; (2) only **L2H2 high-roof** vans are usable (the camper conversion needs standing height); (3) **6 seats is a plus, not a requirement** — seats no longer gate. The **German market** (much better stock/price) and Dutch **financial-lease** stock (buy-upfront price) are now primary inputs. The headline dashboard is the **asking-price L2H2 feed** (`docs/index.html`); auctions are demoted to `docs/auctions.html`.

The **asking feed** (`asking_feed.py`) is now the primary product: it aggregates fixed-price marketplace + lease listings across NL/DE/BE, classifies them against the whitelist, applies the soft-gated filter, scores for camper-conversion suitability, and ranks by within-market price percentile vs the cohort median of the same (model_group, **market**, year ±2) — where *market* is `de` vs `nl`(+be), since German prices run well below Benelux. The confirmed-H2/H3 subset feeds `docs/index.html` (the L2H2 dashboard); the full feed feeds `docs/asking.html`.

The **auction feed** (legacy, secondary) still discovers Troostwijk/Vavato lots, runs the same classifier/cost model, and surfaces hidden gems via Telegram. It shares `van_intel.py`, so it automatically inherits the high-roof families, seats relaxation, and roof scoring. Outputs feed `docs/auctions.html` + `docs/l2h2.html`. All feeds run in one `python3 run.py` invocation.

**Whitelist groups (the only models that pass the classifier):**

| Group key | Models | Required size | min year | Notes |
|-----------|--------|---------------|----------|-------|
| `transit_custom_l2h1` | Ford Transit Custom + Ford Tourneo Custom (passenger) | L2H1 | 2016 | H1 is the only height variant; bare "transit" routes to `ford_transit_l2h2` |
| `expert_jumpy_proace_l2` | Peugeot Expert + Traveller / Citroën Jumpy + SpaceTourer / Toyota ProAce + ProAce Verso | L2, any H | 2016 | EMP2 platform — cargo + passenger trims share the chassis |
| `scudo_gen3` | Fiat Scudo (gen 3, 2022+) | L2, any H | 2022 | Rebadged Expert/Jumpy; separate group to exclude old Scudo (2007-2016) |
| `vivaro_trafic_primastar_l2` | Opel Vivaro / Renault Trafic / Nissan Primastar / Fiat Talento | L2, any H | 2015 | shared NV300 platform; Talento is rebadged Trafic |
| `t6_1_lwb` | VW Transporter T6 + T6.1 (Multivan / Caravelle / California) | L2 (LWB), any H | 2015 | Both T6 and T6.1 are Euro 6 from launch; BiTDI 204hp is T6-only |
| `psa_l1l2h1` | Peugeot Boxer / Fiat Ducato / Citroën Jumper | L2 or L3, any H | 2016 | Sevel-platform; L2H2/L3H2 are the camper gold standard. L1/L4 reject |
| `vito_v_class_l2` | Mercedes Vito / V-Class (Lang or Extralang) | L2 or L3, any H | 2015 | W447 chassis; Kompakt variant rejects via L1 keyword |
| `hyundai_staria` | Hyundai Staria | any | 2021 | Korean MPV, single length (5253mm) |
| `ford_transit_l2h2` | Ford Transit (full-size, **not** Custom) | L2 or L3, any H | 2016 | High-roof pivot; "transit connect/courier" reject via siblings |
| `mercedes_sprinter` | Mercedes Sprinter | L2 or L3, any H | 2016 | High-roof pivot; the global stand-up campervan benchmark |
| `vw_crafter_tge` | VW Crafter (gen 2) / MAN TGE / e-Crafter | L2 or L3, any H | 2017 | High-roof pivot; gen-2 is VW's own platform |
| `renault_master_grp` | Renault Master / Opel Movano / Nissan Interstar (NV400) | L2 or L3, any H | 2016 | High-roof pivot; shared platform |

Height is soft-gated everywhere (unknown passes); the L2H2 feeds filter to a CONFIRMED H2/H3 via `is_high_roof()`. Other van families (Daily, plain Transit Connect, etc.) still reject as `brand_not_in_whitelist`.

**Soft-gate policy** — only *confirmed* violations reject:
- Year known and `< min_year` for group → reject
- Emission known and below Euro 6 → reject
- Size known and outside the group's allowed L/H → reject
- Any of these unknown → PASS

**Seats are NOT gated** (June 2026 pivot): cargo panel vans are the desirable conversion bases, so a 2-seat L2H2 cargo van passes. Seats are a scoring bonus only (6+ = plus). **Roof height is the dominant score factor**: confirmed H2/H3 → big bonus, confirmed H1 → penalty, so a 2-seat L2H2 outranks a 6-seat L1H1.

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
| `kleinanzeigen_de.py` | Kleinanzeigen.de (DE) — server-rendered HTML scrape (`article.aditem`). Works over plain HTTP |
| `mobile_de.py` | mobile.de (DE) — consumer JSON API via curl_cffi, optional `MOBILE_DE_PROXY`. **Akamai-blocked from datacenter/CI IPs** → degrades to `[]`; needs a residential IP/proxy |
| `regeljelease.py` | Regeljelease.nl (NL financial lease) — **upfront purchase price** from embedded vehicle JSON on `/aanbod/<brand>/<model>` SEO pages |
| `financiallease.py` | Financiallease.nl (NL financial lease) — **upfront purchase price** from Magento brand catalog cards (`/aanbod/<brand>`) |
| `rosfinance.py` | Rosfinance.nl (NL lease) — best-effort stub; JS SPA + authenticated API, returns `[]` |
| `market_price.py` | Combined multi-source PriceIndex facade; rotating refresh of the N stalest sources (`max_sources`) |
| `asking_feed.py` | Asking-price aggregator (PRIMARY) — reuses `price_cache.json` to emit a deduped, country-aware cross-source feed of whitelist-matching listings |
| `notify.py` | Telegram alerts for hidden gems closing within 24h |
| `fleet.py` | Fleet-type classification (utility/delivery/solar/telecom/…) |
| `models.py` | Pydantic `Vehicle` dataclass (incl. `model_group`, `variant`, `classification_confidence`) |
| `docs/index.html` | **PRIMARY dashboard** — high-roof L2H2 asking feed (loads `asking_l2h2.json`) |
| `docs/asking.html` | Full asking-price aggregator dashboard (all sources, loads `asking_listings.json`) |
| `docs/auctions.html` | Auction camper-candidate dashboard (legacy, loads `latest.json`) |
| `docs/l2h2.html` | High-roof auction dashboard (loads `l2h2.json`; usually empty — auction height mostly unknown) |
| `docs/overrides.js` | Shared dismiss/bookmark layer for all dashboards — per-card ✕/★ buttons, localStorage state, GitHub-PAT sync to `user_overrides.json` |

---

## Outputs (all in `output/`)

| File | Contents |
|------|---------|
| `latest.json` | Accepted camper candidates from the auction feed, sorted by `score` (small-van suitability) |
| `l2h2.json` | Auction lots with a CONFIRMED high roof (H2/H3 in the size code; unknown-height excluded per no-guessing). Feeds `docs/l2h2.html`. Often empty — most auction lots have unknown height. |
| `asking_listings.json` | Deduped, country-aware cross-source asking-price feed (Marktplaats / AutoScout24 / 2dehands / Autotrack / Kleinanzeigen / mobile.de / NL-lease). Feeds `docs/asking.html`. Sorted underpriced-first. |
| `asking_l2h2.json` | Subset of `asking_listings.json` with a confirmed H2/H3 size code. Feeds the **primary** `docs/index.html`. |
| `rejected.json` | `{url: reason}` map for all rejected vehicles |
| `lot_registry.json` | Per-URL last-scrape state + permanent rejects |
| `bid_history.json` | Hammer history per model token |
| `price_cache.json` | Per-source listings cache populated by `market_price.build_price_index_cached`; read by `asking_feed.py` |
| `user_overrides.json` | Dashboard dismiss/bookmark state — `{dismissed:{url:…}, bookmarked:{url:…}}`. Written by the browser via the GitHub Contents API (PAT); read by `run.py`/`registry.py` (dismissed → `permanent_rejects` reason `user_dismissed`, reconciled every run) and `asking_feed.py` (dismissed dropped from feed). |
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
| Kleinanzeigen.de | DE (C2C + dealer) | server-rendered HTML (`article.aditem`), plain HTTP |
| mobile.de | DE (dealer) | consumer JSON API via curl_cffi (+ optional proxy) — **Akamai-blocked from datacenter/CI IPs** |
| Regeljelease.nl | NL (financial lease) | embedded vehicle JSON on SEO pages — **upfront purchase price** |
| Financiallease.nl | NL (financial lease) | Magento brand catalog cards — **upfront purchase price** |
| Rosfinance.nl | NL (financial lease) | best-effort only (JS SPA + authed API → empty) |

Cohort medians are computed per **market** (`de` vs `nl`+`be`) so German listings aren't all flagged underpriced against Benelux medians. Lease sources contribute the **upfront purchase price** (not the monthly payment) so they're comparable to marketplace asking prices.

mobile.de is reachable only from a residential IP or via `MOBILE_DE_PROXY`; lacentrale.fr blocks headless HTTP (403). The pipeline degrades to `[]` for any blocked source — it never breaks the run.

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

- **mobile.de Akamai block**: mobile.de hard-blocks datacenter/CI IPs (HTTP 403 or a 200 "behavioral content" JS challenge) across plain HTTP, headless Playwright, AND curl_cffi. `mobile_de.py` degrades to `[]` from CI; set `MOBILE_DE_PROXY` (residential proxy) or run locally from a residential IP to get data. `curl_cffi` is a requirement for its TLS impersonation.
- **Seats are not gated**: post-pivot, `strict_filter` never rejects on seats (cargo panel vans are wanted). `seats_below_5` is no longer emitted (kept in `registry.permanent_rejects` recognised set for back-compat). Seats remain a scoring bonus; **roof height (H2/H3) is the dominant score factor** in `score_small_van`.
- **bare "transit"**: routes to `ford_transit_l2h2` (full-size Transit), NOT Transit Custom. "transit custom"/"tourneo custom" win via multi-word sort; "transit connect"/"transit courier" reject via `SMALLER_SIBLINGS`.
- **networkidle timeout**: Must use `domcontentloaded` + `wait_for_selector("script#__NEXT_DATA__", state="attached")`. Never switch back to networkidle.
- **Word-boundary filters**: All keyword lists in `van_intel.py` use `\b` regex boundaries to avoid "bus" matching "business", "partner" matching "business partner", etc.
- **engine failure false positive**: Pattern uses negative lookbehind for "no/geen/kein" and excludes "engine failure codes" (OBD context).
- **LWB → L2 clamp**: "LWB" / "Maxi" / "lang" map to L3 globally (correct for big vans) but are clamped down to L2 inside `classify_vehicle` for whitelist groups whose `required_length=[2]` only, since those families have no L3 variants. The `vito_v_class_l2` group (Mercedes Extralang really IS L3-class) is exempt. Explicit "L3" / "L4" markers still reject for L2-only groups.
- **T6.1 detection**: permissive — any "Transporter" match enters the `t6_1_lwb` group; soft-gates on year (rejects only if `year < 2020` confirmed). Lots without an explicit year pass through.
- **GraphQL parse error**: `Response.json: No resource with given identifier found` is benign — fires when response body isn't buffered yet. Wrapped in try/except.
- **cold-start run time**: First run ~15-20 min on GH Actions (400 URL cap, 4 workers). Subsequent runs 5-10 min.
- **Backward-compat fields**: `models.py` keeps `van_category`, `big_van_score`, `small_van_score` as deprecated unused fields so old `lot_registry.json` snapshots still deserialise.
