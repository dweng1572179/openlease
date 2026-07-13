# OpenLease — corpus export

45,186 commercial-real-estate listings across New York, Miami and Los Angeles, plus the NYC
storefront-vacancy registry. Three formats of the same data — pick the one that fits how you
work.

| File | For | Size |
|---|---|---|
| `openlease-corpus.db.gz` | running the app | ~5 MB |
| `openlease-listings.csv.gz` | a spreadsheet | ~2 MB |
| `openlease-listings.jsonl.gz` | piping into something | ~2 MB |

## Run the app

```bash
gunzip openlease-corpus.db.gz
mv openlease-corpus.db ~/openlease/openlease.db      # clone the repo first
cd ~/openlease && ./run.sh                           # -> http://localhost:8788
```

No API keys needed for anything below: search, the map, parcels, full-text ranking. An
`ANTHROPIC_API_KEY` upgrades plain-English search from the rules-based parser to the LLM;
it is optional.

## What a listing does and does not carry — read this first

The honest shape of the data, because raw counts mislead:

- **~500 listings are fully shoppable** (map pin + square footage + asking rent). Most of
  those are NYC office (Metro Manhattan) and LA industrial (Rexford/WestMac).
- **Most listings have a size but no rent.** This is not a scrape failure — it is the
  business. In CRE, office and industrial asks are usually withheld ("Rent: Upon Request")
  and negotiated. RIPCO, a major retail brokerage, publishes zero rents publicly. A listing
  with an address, a size, a pin and a link back to the broker is still shoppable: filter to
  2,500 SF in a neighbourhood, get a dozen, and call.
- **~44,000 rows are NYC vacancy LEADS** from the City's Storefront Registry — an address
  and a "this ground-floor space is vacant" flag, no size and no rent. They are excluded
  from any search that filters on size or rent, and surface only for "where is there vacant
  retail". Treat them as a lead list, not as listings with terms.

`size_sf IS NOT NULL AND asking_rent IS NOT NULL` is the filter for "fully priced".

## What is NOT in this export

- **Walk Score and Transit Score are absent.** They are computed from OpenStreetMap at
  ingest and were left out to keep the file small. Regenerate them with `POST /api/enrich`
  (idempotent, paced, resumable — it only touches rows with no score yet). Ask the owner for
  a `--with-scores` export if you want them precomputed instead.
- **The owner's workspace** — their saves, portfolios and search history — is deliberately
  excluded. This is the public corpus, not their session.

## Provenance

Every row has a `source` (which broker/feed it came from) and a `source_url` (the exact page
it came from). Nothing here is invented: we store facts read off a public page — address,
size, rent, type — never the broker's marketing prose, and we never re-host their photos.
Check any row against its `source_url`.
