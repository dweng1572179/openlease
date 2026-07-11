# OpenLease — Design Spec

_The open, self-hosted version of SpaceFinder (`spacefinder-teardown.md`). Sibling
to OpenProp: same architecture, different domain (CRE leasing vs. investor property
intelligence)._

**Status:** approved for planning · **Date:** 2026-07-11

Every endpoint, dataset ID, field name, and library version below was verified live
on 2026-07-11 (two research workflows, adversarially checked). Where a source is
partial or fragile, it says so — that is load-bearing, not hedging.

---

## 1. What it is

An AI-native commercial-real-estate leasing search. You describe the space you need
in plain English ("retail in Wynwood ~1,500 SF under $8k/mo"); it returns matching
listings on a map, each enriched with free public data and conversationally
queryable. Target user: CRE brokers / tenant reps. Markets: **NYC, Miami, LA,
Chicago** — the four SpaceFinder serves.

Same promises as OpenProp: **self-hosted, bring-your-own-keys, runs keyless** on free
government data, paid providers only on cache miss, everything cached so you never pay
twice. Keys pasted in a Settings dashboard, not `.env`.

**Naming/infra:** directory `openlease/`, `OPENLEASE_PASSWORD`, default port **8788**
(OpenProp holds 8787, so both run at once). Everything else mirrors OpenProp file-for-file.

---

## 2. The four layers

### Layer 1 — Supply (the moat): a fetch ladder, not 13 scrapers

SpaceFinder's moat is aggregating ~13 broker sites. We replace 13 brittle per-site
parsers with **one generic ladder**, descending only when the rung above is absent:

1. **`robots.txt`** — fetched, parsed, obeyed. `Crawl-delay` honored. No override flag exists.
2. **`sitemap.xml`** — enumerates listing URLs; `<lastmod>` drives recrawl.
3. **Structured feed** — the big win. Many broker sites publish their inventory as
   JSON with no scraping at all:
   - **WordPress REST** — `GET /wp-json/wp/v2/property-listings` (RIPCO: **833 listings**, structured, no auth).
   - **JSON-LD** — `<script type="application/ld+json">` blocks on detail pages.
   - **Buildout plugin JSON** — the syndication backend behind many small-broker sites, delivered client-side.
4. **HTML + LLM extract** — last resort. Scrapling's `Convertor` strips the listing
   container to markdown; one LLM prompt maps it to the normalized `Listing` schema.
   **No per-site CSS parsers.** A site redesign costs nothing; a new site is a URL in
   Settings.

**Fetch engine — Scrapling** (`scrapling[fetchers,ai]==0.4.10`, BSD-3):
- Default tier: `FetcherSession(impersonate='chrome', stealthy_headers=True, retries=3)`
  — curl_cffi, **no browser**, negligible RAM. Handles ~95% of regional broker sites
  (they're server-rendered).
- Stealth tier (**on by default** per product decision): a single long-lived
  `AsyncStealthySession(headless=True, max_pages=2, disable_resources=True,
  solve_cloudflare=True)` for Cloudflare/Vercel-walled sites (CBRE, Colliers, Crexi,
  KSR). On 8GB: **exactly one** browser session for a whole run; never call the
  one-shot `StealthyFetcher.fetch()` in a loop (launches+kills Chromium per call).
  Requires `scrapling install` (~400–600MB Chromium, one-time) — the Settings page
  says so and the app degrades gracefully if it's absent.
- Use Scrapling's **Spider** layer, not a hand-rolled loop: it already provides
  `robots_txt_obey=True` (honors Crawl-delay), `download_delay`,
  `concurrent_requests_per_domain`, checkpointed pause/resume, and a dev cache for
  iterating on extraction against saved HTML.
- Turn on `adaptive=True` for the handful of selectors per site, with
  `storage_args={'storage_file': 'openlease/data/elements.db'}` (default path is
  inside site-packages and dies on reinstall).

**Non-negotiable guardrails** (independent of the stealth decision — these address
*copyright/contract* risk, a different axis than bot-walls, per CoStar v. CREXi 2025):
- **Never authenticate.** No login, account, session cookie, or registration/NDA-gated
  content — ever. This is the one bright line every scraping case that went badly
  crossed (hiQ paid $500k on contract, not CFAA, because it used accounts). Defeating a
  bot-detection WAF on a *public no-login* page is the hiQ-*protected* case; crossing a
  login is not. Stealth-on-default is fine; auth is the wall we never climb.
- **Store facts, not expression.** Persist address, BBL/APN/folio/PIN, SF, ask, type,
  broker name/phone, `source_url`, `first_seen`/`last_seen`. **Never download or
  re-host listing photos** (SpaceFinder puts them on S3; this is exactly the CoStar v.
  CREXi "copy and crop" fact pattern with statutory damages). **Never persist broker
  marketing prose verbatim** — link to it, and write our *own* description with the LLM.
- **Identify honestly:** UA `OpenLeaseBot/0.1 (+<repo>; <email>) self-hosted
  single-user`. Rate-limit 1 req / 3–5s per domain, exp-backoff on 429/503, daily
  per-domain cap. Cache hard (ETag/Last-Modified conditional GETs, TTL ≥ 24h).
- **Per-domain allowlist** (`data/sources.yml`), shipped with the verified regional
  sites (§7). README demos on the open-data connectors, **not** a broker's site.

**Plus free government supply** (zero ToS surface, a lead source no scraper gives you):
- **NYC Storefront Registry** (Socrata `92iy-9c3n`, keyless) — every ground-/2nd-floor
  commercial space with a `vacant_on_12_31` flag, BBL, address, lat/lng, business
  activity. A vacancy lead source.
- **NYC ACRIS** (`bnx9-e6tj`, keyless) — deeds, mortgages, amounts, dates → distress signal.
- **CSV import** — bring your own broker export / CoStar pull.

### Layer 2 — Enrichment: per-metro parcel data + metro-agnostic scoring

**Parcel data — a `ParcelProvider` Protocol, one impl per metro.** All four verified
keyless. `None` means "this market does not publish this field," surfaced in the UI —
**never** conflated with "lookup failed."

| Metro | Source | Join key | Owner | Zoning | Notes |
|---|---|---|---|---|---|
| **NYC** | PLUTO Socrata `64uk-42ks` + GeoSearch→BBL; MAPPLUTO ArcGIS for spatial | BBL | ✅ | ✅ + FAR | `bbl` is a NUMBER col (filter unquoted). Socrata geom is text → no point-in-polygon there; use the ArcGIS FeatureServer for map clicks. |
| **Miami** | Miami-Dade PA `PaGISView_gdb` ArcGIS FeatureServer | 13-digit folio | ✅ | ⚠️ **municipality-split** | County zoning layer returns **0 features** for Brickell/Wynwood/Downtown (incorporated cities). Must branch to `M21_Zoning` (City of Miami) etc., or return `null` zoning — never silently empty. |
| **LA** | LA County Assessor `LACounty_Parcel` MapServer | 10-digit AIN | ❌ **none (statute)** | ⚠️ separate ArcGIS layer, no FAR | Owner-of-record is legally unpublishable for free in CA. LA listings show fewer fields **by design**. |
| **Chicago** | Cook County Socrata (`3723-97qp`→PIN, `pabr-t5kh` attrs) + City zoning `7cve-jgbp` | 14-digit PIN | ✅ | ⚠️ **city-only** | Zoning/floors/FAR are City of Chicago; **null for ~half the county** (suburbs). Return `null` + reason, never garbage. |

Normalized internal shape: `parcel_id, owner_name|null, zoning|null, far_built|null,
far_allowed|null, year_built, lot_sqft, bldg_sqft, floors, units, use_code`.

Address→parcel geocoding: NYC uses free keyless **GeoSearch** (`geosearch.planninglabs.nyc`);
the other three use their own ArcGIS/Socrata address search (§7). PLUTO/parcel data
refreshes ~2×/year — cache by parcel key indefinitely; a nightly refresh is wasted effort.

**Metro-agnostic enrichment (single implementation, all keyless):**

- **POIs — Overpass** (`overpass-api.de`, radius 2414m), **at ingest only, cached
  forever** (buildings don't move; Overpass 429/504s under request-time load). Query
  with `nwr` (not `node` — misses malls/parks that are ways) + `out center tags`.
  **An empty Overpass response is an ERROR, never a score of 0** (`overpass.osm.ch` is
  a Switzerland-only extract that returns 200 + zero elements for US coords and
  silently scores everything 0 — allowlist only `overpass-api.de` / `overpass.kumi.systems`).
- **Walk Score** — reimplement the **published 2011 Walk Score methodology** (not a
  hand-rolled heuristic). 9 categories, weights summing to 15, ×6.67 → 0–100.
  `decay(d) = ((2414-d)/2012)^2.3135`, clamped 1.0 below 402m, 0 above 2414m (solved
  from Walk Score's three published anchors). **Validated: Empire State Bldg = 100,
  Bay Ridge = 98**, matching Walk Score's own published values. The per-category
  breakdown is a listing-page UI element ("explains the score instead of asserting it").
- **Transit Score** — also published. Aggregate **per route** (not per stop): `Σ
  routes (trips/wk × mode_weight × decay(nearest stop on route))`, log-normalized.
  Mode weights: rail 2, ferry 1.5, bus 1. Rail from bundled data; bus from Overpass
  `route_ref`. Normalization constant needs calibration against ~20 known addresses.
- **Rail stations — bundled static JSON**, not an API. NYC 496 + Miami 44 + LA 111 +
  Chicago 145 ≈ **800 stations, <100KB total**. Zero API calls, zero failure modes;
  a build-time refresh script regenerates from each agency's open data / GTFS.
- **Airports — OSRM `/table`** (`router.project-osrm.org`, one keyless call returns all
  metro airports). Free-flow (no traffic) — **label it "no traffic" in the UI** (its
  Midtown→JFK 31min vs. real 45–60). Offline fallback: fitted power law
  `drive_min = 5.31 × haversine_mi^0.718` (underestimates bridge/water routes).
- **Street View** — Google embed **only if `GOOGLE_MAPS_KEY` is set**; else omitted.

### Layer 3 — Search: LLM parse → hard filter → hybrid rank → LLM reply

Mirrors SpaceFinder's `POST /api/search` **verbatim as the wire contract**:
- **Request** `{ message, priorState, sessionId, metro }` — `metro` ∈ `nyc|mia|la|chi`;
  `sessionId` keys a `search_session` row (the "Recent" chat history); `priorState`
  carries the prior `query.mustHaves` so a follow-up ("make it bigger, drop the rent
  cap") refines instead of restarting.
- **Response** `{ query, results[], reply, isNearMiss, suggestions[] }`.

1. **Parse** — `anthropic.messages.parse()` → `query.mustHaves`, mirroring SpaceFinder's
   field names: `propertyTypes[]`, `transactionType` (`lease|sale`), `boroughs[]`,
   `neighborhood`, `minSizeSf`/`maxSizeSf`, `maxRentPerSfYr`, `minLat/maxLat/minLng/maxLng`,
   `excludeAddrStates[]`, `excludeZip3[]`, `excludeCities[]`. **Sentinels, not nulls** —
   the hard-won OpenProp lesson: >16 nullable params → 400; *any* optional param → the
   request **hangs** (2^N grammar shapes). All fields required in the schema; `""`/`0`/`[]`
   mean "not mentioned," dropped before they become filters. Unit conversion lives in the
   prompt: "under $12k/mo" + parsed SF → `maxRentPerSfYr` (≈ $12k×12 ÷ SF). "West Village"
   → bbox + `boroughs` + the `excludeCities` suburb list that scopes to the metro. No key
   → rules parser, **loudly logged** (a silent fallback hid a 400 for OpenProp's whole life).
2. **Filter** — `propertyTypes` / SF range / `maxRentPerSfYr` / bbox / `metro` /
   `transactionType` become the **SQL WHERE**; the `exclude*` lists become `NOT IN`
   guards. Hard constraints, never soft-ranked away.
3. **Rank** — over the filtered survivors only:
   - **FTS5 `bm25()`** — compiled into every stock Python (verified: python.org 3.12,
     system, pyenv, Homebrew, Docker slim). `bm25()` is **NEGATIVE** → `ORDER BY ... ASC`.
     External-content table + sync triggers. Tokenize the LLM's parsed keywords and
     quote each (raw prose in `MATCH` throws on a stray apostrophe).
   - **Optional cosine** — when `VOYAGE_API_KEY` set: `voyage-4-lite` 1024-dim (200M
     free tokens; our ~5k-listing corpus is 0.5M — free forever). Vectors stored as
     float32 SQLite BLOBs, L2-normalized at write, brute-force `M @ q` in numpy
     (**0.84ms over 5000×1024** — no vector index needed).
   - **Fuse with RRF, k=60** (Cormack/Clarke/Büttcher SIGIR'09), **not weighted sum**
     (BM25 is unbounded/negative, cosine is [-1,1] — incomparable scales). RRF over
     **one** list is order-preserving, so **keyless degrades to pure BM25 with zero
     branching** in the ranker. Each result carries `semanticScore` (0–1 off the fused
     rank), a composite `score`, and a one-line `rationale` (why it matched) — SpaceFinder's
     three per-listing ranking fields — so the contract is identical with or without a key.
   - **NOT sqlite-vec:** needs `enable_load_extension`, **absent on stock python.org
     macOS / pyenv / system python** (present only on Homebrew + Docker). Would work in
     the container and break on the user's Mac — the worst failure mode — and buys
     nothing at 5k rows.
4. **Reply** — LLM writes the conversational summary highlighting top matches +
   `isNearMiss` handling + follow-up `suggestions`.

### Layer 4 — AI features + workspace

- **Per-listing RAG chat** — `POST /api/listings/{id}/ask` `{question, history}`,
  grounded in that listing's enriched record. No chunking; the record fits one prompt.
- **AI Highlights + "About the property" narrative** — generated once from the combined
  data, cached (this is also how we avoid persisting broker prose — we write our own).
- **Portfolios** (client shortlists) + **saves/favorites**.
- **Export** CSV/XLSX (lift OpenProp's `export.py`).
- **Map** — MapLibre: pins, pan/zoom, **address lookup**, **draw-area polygon search**,
  "saved only" filter, metro switcher.

---

## 3. Architecture

```
Browser (HTMX + Tailwind CDN + MapLibre)
   └─ openlease (FastAPI, :8788) ── same-origin /api/*
        ├─ POST /api/search              {message,priorState,sessionId,metro} → {query,results,reply,isNearMiss,suggestions}
        ├─ POST /api/listings/{id}/ask   per-listing RAG chat {question,history}
        ├─ GET  /api/listings/{id}       enriched detail
        ├─ GET  /api/sessions            "Recent" search history (sessionId-keyed)
        ├─ POST /api/crawl               run the fetch ladder over sources.yml (admin)
        ├─ portfolios / saves / export / settings
        └─ server-side: SQLite (listings + parcels + vectors + sessions + cache + portfolios)
   ├─ overpass-api.de        POIs (ingest-time)
   ├─ router.project-osrm.org airport drive times
   ├─ {metro parcel APIs}    PLUTO / Miami PA / LA Assessor / Cook County
   ├─ api.voyageai.com       embeddings (optional key)
   ├─ api.anthropic.com      parse / reply / extract / chat (optional key)
   └─ broker sites           via Scrapling fetch ladder (allowlisted)
```

## 4. Layout (mirrors OpenProp)

```
openlease/
  app/
    app.py            FastAPI, auth (one password + signed cookie), home
    config.py         .env settings (+ inline-comment guard, lifted from openprop)
    db.py             SQLite schema + persistence
    models.py         Listing / ListingQuery / Parcel
    cache.py          cache-through + monthly budget guardrail   [lift from openprop]
    registry.py       capability → provider; parcel_provider(metro)
    settings_store.py dashboard-saved keys override .env          [lift from openprop]
    crawl.py          the fetch ladder (Scrapling Spider)
    extract.py        HTML/feed → Listing (LLM + structured-feed fast paths)
    score.py          walk + transit score (published methodology)   [self-check]
    rank.py           FTS5 BM25 + optional cosine + RRF               [self-check]
    ai.py             NL→ListingQuery, reply, highlights, per-listing chat
    routes_search.py  routes_listings.py  routes_portfolios.py
    routes_crawl.py   routes_settings.py  routes_export.py
    providers/
      base.py         Protocols: ParcelProvider, PoiProvider, Geocoder, Embedder
      parcel_nyc.py  parcel_miami.py  parcel_la.py  parcel_chicago.py
      geosearch.py   overpass.py  osrm.py  voyage.py
    data/
      metros.yml           bbox, airports, zoning-source branches per metro
      sources.yml          allowlisted broker sites + fetch-ladder rung + robots status
      rail/*.json          bundled station points, 4 metros
      elements.db          Scrapling adaptive-selector store
    templates/        Jinja + HTMX + Tailwind(CDN) + MapLibre
  tests/
    test_score.py     walk/transit vs. known anchors (ESB=100, Bay Ridge=98)
    test_rank.py      BM25 order (ASC), RRF single-list == passthrough, RRF fuse
    test_extract.py   feed fast-paths (wp-json, JSON-LD) on canned fixtures
    test_parcel.py    each metro's field-normalization + null-not-fail on missing owner
    test_smoke.py     auth + search + listing card + portfolio (end-to-end, keyless)
  requirements.txt  run.sh  Dockerfile  docker-compose.yml  README.md
  Start OpenLease.command / .bat  (double-click launchers, from openprop)
  .env.example
```

## 5. Data model (SQLite, WAL, stdlib sqlite3 — no ORM)

Columns stored `snake_case`, **serialized to SpaceFinder's `camelCase` at the API
boundary** so `/api/search` results are field-for-field compatible with the teardown's
observed listing object (`sizeSf`, `divisibleMinSf`, `ceilingHeightFt`, …).

- `listing` — the ~35-field observed schema, minus the copyright traps:
  `id, source, source_url, status, metro, property_type, subtype, transaction_type,
  address, neighborhood, borough, lat, lng, size_sf, divisible_min_sf, divisible_max_sf,
  total_building_sf, floor, ceiling_height_ft, asking_rent, rent_unit, lease_type,
  sale_price, availability_date, lease_term_months, condition, broker_name, broker_firm,
  broker_phone, broker_email, features_json, brochure_url, our_description (LLM-written),
  highlights_json (LLM), photo_urls_json (external hot-link references — see below),
  parcel_id, walk_score, transit_score, score_breakdown_json, semantic_score, score,
  rationale, first_seen, last_seen`, UNIQUE(source_url).
  - **Two deliberate divergences from SpaceFinder** (CoStar v. CREXi, §2): (1) no
    `description` column — the broker's marketing prose is **never persisted**; we serve
    `our_description` (LLM-written) and link `source_url` for the original. (2) `photo_urls`
    are stored as the broker's own URLs and **referenced, never downloaded/re-hosted**
    (SpaceFinder mirrors them to S3; we do not). At the API boundary `our_description`
    serializes as `description` and `photo_urls` as `photos[]` so the client is unchanged.
- `search_session` — id (= `sessionId`), metro, title, created_at; `search_turn` —
  session_id, message, mustHaves_json, reply, created_at. Backs "Recent" history and
  `priorState` follow-up refinement.
- `listing_fts` — external-content FTS5 (address, our_description, neighborhood) + triggers.
- `listing_vec` — listing_id, embedding BLOB (float32, L2-normed). Present only with a key.
- `parcel` — parcel_id (metro-prefixed), metro, owner_name?, zoning?, far_built?,
  far_allowed?, year_built, lot_sqft, bldg_sqft, floors, units, use_code, raw_json.
- `poi` / `transit_nearby` — cached Overpass + nearest-station results per listing.
- `portfolio` / `portfolio_item` / `saved` — workspace.
- `chat` — listing_id, role, content (per-listing RAG history, `/api/listings/{id}/ask`).
- `provider_cache` — **lifted verbatim from OpenProp** (request_hash UNIQUE, cost_cents,
  monthly spend guardrail).
- `setting` — dashboard-saved keys.

Note `transaction_type` + `sale_price`: SpaceFinder handles **both lease and sale**;
so do we. The default filter is `lease`, but a "for sale" query flips it.

## 6. Keyless vs. keyed

| Works keyless | Unlocked by a key |
|---|---|
| All 4 metros' parcel data | `ANTHROPIC_API_KEY`: NL parse, conversational reply, LLM extraction, highlights, per-listing chat |
| Overpass POIs, walk + transit scores | `VOYAGE_API_KEY`: semantic ranking (free tier covers corpus 400×) |
| Bundled transit, OSRM airports | `GOOGLE_MAPS_KEY`: Street View embed |
| **BM25 search**, crawler, CSV import | (`scrapling install`: stealth tier for Cloudflare-walled sites) |
| Portfolios, saves, export, map | |

Budget guardrail (OpenProp's `cache.py`): monthly paid-spend cap refuses paid calls
past the cap, shows running spend. The only paid surfaces are Anthropic + Voyage;
everything else is free.

---

## 7. Verified data appendix (as of 2026-07-11)

**NYC** — bbox `40.4774,-74.2591,40.9176,-73.7002` · airports JFK/LGA/EWR
- Parcel: `data.cityofnewyork.us/resource/64uk-42ks.json?bbl=<bbl>` (unquoted) ·
  spatial: `services5.arcgis.com/GfwWNkhOj9bNBqoJ/.../MAPPLUTO/FeatureServer/0/query`
- Geocode: `geosearch.planninglabs.nyc/v2/search?text=<addr>` → `addendum.pad.bbl`
- Rail: `data.ny.gov/resource/39hk-dx4f.json` (496) · Storefront `92iy-9c3n` · ACRIS `bnx9-e6tj`
- Brokers: ripcony.com (wp-json feed, 833), rtl-re.com/listings, metro-manhattan.com,
  ksrny.com (429-throttles), nycretailleasing.com (403→stealth)

**Miami** — bbox `25.55,-80.50,25.98,-80.10` · airports MIA/FLL
- Parcel: `services.arcgis.com/8Pc9XBTAsYuxx9Ny/.../PaGISView_gdb/FeatureServer/0/query`
  (`TRUE_SITE_ADDR LIKE`, 13-digit folio; owner `TRUE_OWNER1`)
- Zoning branch per municipality (`M21_Zoning` for City of Miami; county layer is
  unincorporated-only)
- Rail: MetroRail `MetroRailStations_gdb` (23) + Metromover (21) = 44
- Brokers: metro1.com/listings, comras.com, terranovacorp.com, blancacre.com,
  comreal.com/findcomrealproperties, ripcony.com?locations=Miami

**LA** — bbox `33.70,-118.70,34.35,-117.85` · airports LAX/BUR/LGB/SNA (+ONT for industrial)
- Parcel: `public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0/query`
  (10-digit AIN; **no owner field — CA statute**)
- Rail: LA Metro GTFS `gitlab.com/LACMTA/gtfs_rail` → 111 stations (location_type=1)
- Brokers (SSR, crawl first): avisonyoung.us/web/los-angeles/properties-for-lease,
  rexfordindustrial.com/properties, westmac.com/listings · (JS/stealth): kidder.com,
  lee-associates.com (Buildout backend)

**Chicago** — bbox `41.469,-88.264,42.154,-87.524` · airports ORD/MDW
- Parcel: `datacatalog.cookcountyil.gov/resource/3723-97qp.json` (addr→PIN) +
  `pabr-t5kh` (attrs, `class` = bldg class+land use) · zoning `data.cityofchicago.org/resource/7cve-jgbp.json`
  (**City only — null for suburbs**)
- Rail: `data.cityofchicago.org/resource/3tzw-cg4m.json` CTA 'L' (145) [+ Metra 241 optional]
- Brokers: avisonyoung.us/web/chicago/properties-for-lease,
  midamericagrp.com/property-listings?saleOrLease=lease, baumrealty.com/properties,
  svnchicago.com/properties

_Full field-name mappings and sample responses: `scratchpad/metro-data.json` (research artifact)._

---

## 8. Scope boundaries (YAGNI)

**In:** the four layers above, four metros, keyless-first, Settings dashboard, launchers,
a `guide/` walkthrough (mirroring OpenProp), PolyForm Noncommercial license.

**Out (v1):** realtime GTFS, Metra/commuter rail, non-NYC storefront-vacancy feeds
(only NYC publishes one), user accounts beyond the single password, any paid listing
feed (none exist — CRE has no MLS by design), model training on scraped content
(explicitly forbidden), public mirror/republish (local-and-personal only).

**Explicitly rejected, with reasons above:** sqlite-vec, weighted-sum fusion,
per-site scrapers, re-hosting photos, persisting broker prose verbatim, request-time
Overpass, `overpass.osm.ch`, crossing any login.
