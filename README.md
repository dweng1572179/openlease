# OpenLease

An AI-native commercial-real-estate leasing search you host yourself. Describe the space
you need in plain English — *"retail in Wynwood ~1,500 SF under $8k/mo"* — and get matching
listings on a map, each enriched with free public data and conversationally queryable.

Three markets: **New York, Miami, Los Angeles**.

Chicago is built — Cook County parcels, CTA rail, scoring, the lot — and it isn't shipped,
because it has no supply. Every Chicago brokerage we could find puts its inventory behind a
JavaScript search app instead of a feed or a sitemap, and OpenLease doesn't write per-site
scrapers. A market with nothing in it looks broken, so it isn't in the switcher. Import a
Chicago CSV and it works; find a Chicago source the generic ladder can read, add it to
`app/data/sources.yml`, and flip `shipped: true` in `app/data/metros.yml`.

## It runs with no API keys at all

Everything below is free, keyless, and government-sourced:

| Works with no key | Unlocked by a key you bring |
|---|---|
| Parcel data — all four metros, `null` with a reason where a metro doesn't publish a field | `ANTHROPIC_API_KEY` — plain-English search, conversational replies, per-listing chat, LLM extraction from unstructured listing pages |
| Walk Score + Transit Score (published methodology, computed from OpenStreetMap) | `VOYAGE_API_KEY` — semantic ranking (the free tier covers this corpus ~400×) |
| Bundled transit stations, airport drive times | `GOOGLE_MAPS_KEY` — Street View embed |
| Full-text search + ranking (SQLite FTS5/BM25), the crawler, CSV import, the NYC storefront-vacancy import | |
| Portfolios, saves, CSV/XLSX export, the map | |

Keys are pasted on the **Settings** page, not into a file. Paid calls are capped by a
monthly budget you set, and every response is cached — you never pay for the same call twice.
**Nothing is required.** A rules-based parser handles search with no Anthropic key; it
understands far less, and it says so, loudly, rather than quietly dropping half your query.

### How the crawler gets facts without a key

Broker sites publish inventory in one of three ways, and the crawler descends only as far
as it has to:

1. **A structured feed** — the site's own WordPress REST API. Address, broker, a link back.
   No scraping at all.
2. **JSON-LD** — a `<script type="application/ld+json">` block on the detail page.
3. **The page's own text** — "1,500 SF", "$95/SF/yr". Most broker sites are only this.

Rung 3 is what makes it work with no key, and it's the one people skip. Reading a number
off a page is not a per-site scraper — there's no CSS selector anywhere in this codebase, it
works on any site, and a redesign costs nothing. And a number is a *fact*: we're not copying
anyone's writing. **Size and asking rent live in that text almost everywhere**, so without
this rung a keyless crawl produces a link directory — addresses with no SF and no ask, which
a search for "~1,500 SF under $8k/mo" cannot filter on at all.

An `ANTHROPIC_API_KEY` adds a fourth rung for pages too unstructured for any of the above.
It is an improvement, not a requirement.

## Run it

**macOS:** double-click **Start OpenLease.command**. That's it.
**Windows:** double-click **Start OpenLease.bat**.

Or by hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set OPENLEASE_PASSWORD
.venv/bin/python -m app.seed  # 12 demo listings, so there's something to search
./run.sh                      # -> http://localhost:8788
```

Docker: `docker compose up` → http://localhost:8788

OpenLease runs on **8788**, so it and [OpenProp](https://github.com/dweng1572179/openprop)
(8787) can run side by side.

## Try it — the search this README is actually describing

This isn't a hypothetical demo. With the seed data loaded and no API key configured, pick
**Miami** as the metro and search:

> retail in Wynwood ~1,500 SF under $8k/mo

Here's what actually happens: $8,000/mo implies a rent cap of **$64/SF/yr** (`8000 × 12 ÷
1500`). The one listing that otherwise matches — 2618 NW 2nd Ave, Wynwood, 1,500 SF retail —
asks **$95/SF/yr**. A hard filter would return nothing, so OpenLease relaxes the softest
constraint (the rent cap), re-runs the query, and says so: a **near-miss banner** that
plainly discloses it relaxed the rent, not a result quietly passed off as a match. You get
one result card, one pin on the map, and a listing page with a parcel panel (a real,
request-time lookup — for this particular address it comes back "No parcel matched," which
is itself an honest answer, not a crash) and a Save button — no key anywhere. (Walk Score
and Transit Score are computed during enrichment, which runs for crawled and imported
listings; the raw seed data used in this walkthrough hasn't been through that step, so
those two rows are simply absent from the demo listing rather than shown as a fake zero.)

## Where the listings come from

Three supplies, in order of how much you should trust them:

1. **Free government feeds** — no terms-of-service surface at all. NYC's Storefront
   Registry publishes a *vacancy flag* on every ground-floor commercial space in the city —
   **43,978 vacant storefronts**, citywide, as of this writing — via `POST
   /api/import/storefronts`. That's a lead source no broker site has.
2. **Your own CSV** — a broker export, a CoStar pull, whatever you already licensed:
   `POST /api/import/csv`.
3. **The crawler** — `POST /api/crawl`, over the allowlist in `app/data/sources.yml`.

Ingest is two steps, on purpose:

```
POST /api/crawl      # fetch supply — fast
POST /api/enrich     # Walk/Transit score it — slow, paced
```

Scoring calls OpenStreetMap's free Overpass mirrors, which rate-limit hard. Doing it inside
the crawl loop made supply hostage to POI lookups: a measured run spent 30 minutes backing
off and never got past New York. Split apart, the same crawl finishes in nine. Both are
still ingest-time — Overpass is never called while you're searching.

### What the crawler will and won't do

It obeys `robots.txt` (including `Crawl-delay`), asks for it under its own honest identity,
and applies that same identity to every request that follows. It rate-limits to one request
every 3–5 seconds per domain, backs off exponentially on 429/503, caps requests per domain
per day, and uses conditional GETs so a re-crawl mostly costs nothing.

**It never logs in.** Not to any site, not ever, and there is no flag to make it. No
account, no cookie, no registration- or NDA-gated page — stealth fetching is for getting
past a bot-wall on a page that's already public; it never crosses an authentication wall.
Every scraping case that ended badly ended there.

It stores **facts, not expression**: address, size, ask, type, broker contact, and the link
back to the original. It does **not** copy the broker's marketing prose (the descriptions
you read here are written from the facts by OpenLease) and it does **not** download or
re-host their photos (they're hot-linked from the broker's own server, and the listing page
links you to their page). This is the same fact-pattern that CoStar successfully argued
against CREXi.

And it would rather tell you nothing than tell you something wrong. A broker's feed is often
national: RIPCO's returns properties in Cleburne, Texas and Panama City, Florida alongside
Manhattan. A geocoder scoped to one metro *does not decline* — ask NYC's for a street in
Stony Brook and it will confidently hand you a different street in Brooklyn, with the same
confidence score it reports for a correct hit. So the crawler reads the state off the
listing's own URL before it geocodes anything, drops what's out of market, and checks that
the street it asked for is the street it got back. A listing it can't place keeps its address
and its link and simply has **no pin** — rather than a plausible pin in the wrong city.

This is local-and-personal software. Don't republish what it collects.

## The searching

Plain English goes to an LLM, which turns it into hard constraints (type, size band, rent
cap, bounding box). Those become a SQL `WHERE` — a constraint is a constraint, never a
preference that gets ranked away. Whatever survives is ranked by full-text relevance
(SQLite FTS5/BM25) and, if you brought a Voyage key, semantic similarity, fused with
Reciprocal Rank Fusion (k=60). Then the LLM writes the reply.

With no Anthropic key, a rules-based parser takes over instead.

## The scores

Walk Score is **Walk Score's own published 2011 methodology**, computed here from
OpenStreetMap: nine amenity categories, distance-decayed, normalized to 0–100. Checked
against Walk Score's own published anchors: **Empire State Building = 100** (reproduces
exactly) and **Bay Ridge, Brooklyn = 95** — Walk Score itself publishes 98 for Bay Ridge, so
this is 3 points low. That gap is real and it's disclosed, not rounded away: it comes from
OpenStreetMap's POI density in Bay Ridge's weaker categories (parks, books, schools) falling
short of whatever proprietary database Walk Score used in 2011 — not an error in the decay
curve or the weights. A third anchor, an industrial control block in **Vernon, LA = 18**,
proves the score actually discriminates rather than defaulting everything near 100. Every
listing page shows the per-category breakdown, so it explains the score instead of
asserting it.

**Transit Score is not calibrated.** It aggregates trips **per route**, not per stop, which
is the right shape — but its normalization constant (`TRANSIT_NORM`) and its per-mode
trips-per-week figures are eyeballed, not fit against any published methodology or verified
ground truth. They are the only two unpublished, uncalibrated constants in the app. Treat
Transit Score as **a ranking, not a rating** — good for saying "this one has more transit
than that one," not for quoting as a number. The UI says exactly that next to every score.

Airport drive times come from OSRM and are **free-flow — no traffic**. The UI says so.

## What a metro doesn't publish, it says it doesn't publish

LA County does not publish owner-of-record for free (California statute). Chicago's zoning
dataset is the *City's* — it's blank for suburban Cook County. Miami's county zoning layer
returns nothing inside incorporated cities like Wynwood and Brickell, so we branch to the
municipal layer, and it's still blank outside both.

In every one of those cases the field reads **"not published here"** with the reason on
hover. It is never a blank, and never a zero. A tool that guesses is worse than a tool that
admits it doesn't know.

## License

PolyForm Noncommercial 1.0.0 — see `LICENSE.md`. Use it for anything except selling it.
