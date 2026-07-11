# SpaceFinder (spacefinder.ai) — Full Teardown (maximal)

_Hands-on walkthrough of the authenticated app + live `/api` capture (search, listing, auth) + infra fingerprinting, 2026-07-11._

## 1. What it is

SpaceFinder is an **AI-native commercial real estate (CRE) leasing search** — a ChatGPT-style chat where you describe the space you need in plain English ("retail in the West Village ~1,500 SF under $12k/mo") and it returns matching NYC commercial listings on a Mapbox map, each heavily enriched and conversationally queryable.

**Target user:** CRE **brokers / tenant reps** (and tenants). The Portfolios empty-state gives it away: _"add it to a portfolio for your client — then tour it when the shortlist's ready."_

**Markets:** New York (primary), **Miami, Los Angeles, Chicago** (market switcher; `metro` param). **Business model:** no paywall/credits observed — appears free / early-stage.

## 2. The tools / features

- **AI chat search** (left panel): NL → matching listings + a conversational reply; chat history ("Recent"), "New chat", suggested prompts, follow-up refinement via conversation state.
- **Map** (Mapbox): listing pins, **"Look up an address"** (geocoding), **"Draw area"** (polygon search), **"Saved only"** filter.
- **Listing cards:** type badge, neighborhood, address, rent, size, **+ Add to portfolio**, **Save**.
- **Listing detail** (`/listing/{id}`) — heavily enriched (see §4): AI Highlights + narrative, walk/transit scores, transit & airports, nearby POIs, full property facts (PLUTO), broker contact, brochure download, source link, and a per-listing **"Ask SpaceFinder AI"** RAG chatbot.
- **Portfolios:** client shortlists ("✨ New portfolio"). **Save/favorites**, **market switcher**, account menu.

## 3. How the AI search works (the core)

`POST /api/search` runs an LLM pipeline and returns `{ query, results, reply, isNearMiss, suggestions }`:

1. **LLM parse → structured query** with real semantic understanding + unit conversion: "around 3,000 SF" → `minSizeSf 2500 / maxSizeSf 3500`; "under 20k" (monthly) → `maxRentPerSfYr 67` (converted using the parsed size); "West Village"/"Manhattan" → `boroughs`/geo bbox + a long `excludeCities` suburb list.
2. **Retrieve + rank** by a **semantic/vector score** (`semanticScore`) plus a composite `score` — embeddings-based RAG retrieval, not just SQL filters.
3. **LLM reply** — a conversational summary naming the top matches (_"…Lexington Ave Suite 2320 stands out at 3,078 SF…"_), plus `isNearMiss` handling and follow-up `suggestions`.

**Per-listing chatbot:** `POST /api/listings/{id}/ask` with `{ question, history }` → RAG answer grounded in that listing's enriched details.

## 4. Listing enrichment & data sources (the real moat)

**Supply — listings aggregated from ~13+ NYC broker & niche sites** (via `source`/`sourceUrl` + id prefix `web-<source>-<hash>`): metro-manhattan, RIPCO (`ripcony.com`), KSR (`ksrny.com`), Cushman & Wakefield, Newmark (`nmrk.com`), CBRE, Colliers, Lee Associates, Redwood NYC, Meridian, plus retail/medical specialists — `rtl-re.com`, `nycretailleasing.com`, `medicalrealestate.com`, Wexler Healthcare Properties. Broker contact + photos are extracted; **photos/brochures re-hosted on AWS S3**.

**Enrichment layers:**
- **NYC PLUTO** (NYC Dept. of City Planning, free): owner of record, zoning, FAR, year built, building class, lot size, units, land use, historic district — explicitly credited.
- **Walk Score / Transit Score:** *computed by SpaceFinder* "from nearby amenity & transit density."
- **Transit:** nearest subway lines, bus stops, airports w/ walk/drive times (MTA GTFS + OSM-class).
- **Nearby POIs:** dining/shops/parks w/ exact distances (OSM/Places-class).
- **AI-generated content:** listing "Highlights" + "About the property" narrative (LLM-written).
- **Street View:** Google Street View embed on listing pages.

## 5. Architecture / infrastructure

```
Browser (Next.js App Router + React Server Components)  — ?_rsc= prefetch, _next/…?dpl=dpl_… (Vercel deploy IDs)
   ├─ spacefinder.ai (app + /api/* routes) ── hosted on VERCEL
   │     ├─ POST /api/search              (LLM parse + vector retrieval + LLM reply)
   │     ├─ POST /api/listings/{id}/ask   (per-listing RAG chatbot)
   │     ├─ GET  /api/auth/me             (session/auth)
   │     └─ (portfolios / save routes)
   │        server-side: LLM + embeddings + listings DB (enriched w/ PLUTO)  ← not exposed client-side
   ├─ api.mapbox.com / events.mapbox.com  (tiles, fonts, geocoding; public token)
   ├─ www.google.com                      (Street View embed)
   └─ s3.amazonaws.com                    (listing photos + brochures)
```

- **Frontend:** Next.js **App Router + RSC** on **Vercel** (`server: Vercel`, `x-vercel-id`, `dpl_` deploy IDs). `lev.com`-style RSC prefetch via `?_rsc=`.
- **APIs:** Next.js route handlers (`/api/*`), clean same-origin surface; no separate API host.
- **Backend (server-side, not exposed):** an LLM for parse + summaries + per-listing RAG; an embeddings/vector layer for `semanticScore`; a listings DB enriched with PLUTO. No `NEXT_PUBLIC_` keys or vendor hints leak client-side (good separation) → most likely Postgres+pgvector + a hosted LLM, but that's inference.
- **Maps:** Mapbox (public client-side token); **Street View** via Google; **photos/brochures** on S3. **Auth:** session-based (`/api/auth/me`).

## 6. API endpoints & full schemas (observed)

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/search` | Conversational search (schema below) |
| POST | `/api/listings/{id}/ask` | Per-listing RAG Q&A: `{question, history}` |
| GET | `/api/auth/me` | Current user / session |
| GET | `/listing/{id}` · `/portfolios` | RSC-rendered pages (`?_rsc=`) |
| (impl.) | portfolio + save/favorite routes | CRUD for portfolios & saved listings |

**`POST /api/search`**
- **Request:** `{ message, priorState, sessionId, metro }` (`metro` ∈ nyc/mia/la/chi; `priorState` carries conversation state for follow-ups).
- **Response:** `{ query, results[], reply, isNearMiss, suggestions[] }`.
- **`query.mustHaves`** (LLM-extracted; only fields the message implies are populated):
  `propertyTypes[]`, `transactionType` (lease/sale), `boroughs[]`, `neighborhood`, `minSizeSf`/`maxSizeSf`, `maxRentPerSfYr`, `minLat/maxLat/minLng/maxLng` (metro bbox), `excludeAddrStates` (NJ/CT/PA), `excludeZip3[]`, `excludeCities[]`.

**Listing object (~35 fields):**
`id, source, sourceUrl, status, propertyType, subtype, transactionType, address, neighborhood, borough, lat, lng, sizeSf, divisibleMinSf, divisibleMaxSf, totalBuildingSf, floor, ceilingHeightFt, askingRent, rentUnit, leaseType, salePrice, availabilityDate, leaseTermMonths, condition, description, photos[], brokerName, brokerFirm, brokerPhone, brokerEmail, features[], brochureUrl, semanticScore, score, rationale`.
- Sample: `{ id:"web-metro-manhattan-fa78c140f0f9", source:"metro-manhattan", propertyType:"office", address:"15 West 28th Street", neighborhood:"Midtown South", sizeSf:2500, ceilingHeightFt:12, askingRent:10208, rentUnit:"mo" }`.

## 7. One-line summary

A Next.js/RSC-on-Vercel chat app that **aggregates NYC (+ Miami/LA/Chicago) commercial listings scraped from ~13 broker sites**, enriches each with free public data (NYC PLUTO, transit, POIs, computed walk/transit scores) and LLM-written summaries, then serves them through an **LLM-parse → vector-retrieval → LLM-reply** search (`/api/search`) plus a per-listing RAG chatbot (`/api/listings/{id}/ask`) and client "portfolios." The moat is the **aggregation + enrichment pipeline**, not the (thin, elegant) UI. Maps = Mapbox, Street View = Google, photos = S3.
