# OpenLease

The open, self-hosted version of SpaceFinder: an **AI-native commercial-real-estate leasing
search**. Describe the space you need in plain English — *"retail in Wynwood ~1,500 SF under
$8k/mo"* — and get matching listings on a map, each enriched with free public data and
conversationally queryable.

Four markets: **New York, Miami, Los Angeles, Chicago**.

> **Status: designed, not yet built.** This repo currently holds the teardown, the design
> spec, and a task-by-task implementation plan. The application lands at the repo root
> (`app/`, `tests/`) as the plan is executed.

## What's here

| Document | What it is |
|---|---|
| [`docs/design-spec.md`](docs/design-spec.md) | The design. Every endpoint, dataset ID, field name and library version was verified live; where a source is fragile, it says so. |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | 14 tasks, ~110 steps, TDD throughout. Written to be executed by someone (or something) with no prior context. |
| [`docs/spacefinder-teardown.md`](docs/spacefinder-teardown.md) | The teardown of the commercial product this is modeled on — observed API surface and data model. |

## The idea in four layers

1. **Supply.** SpaceFinder's moat is aggregating ~13 broker sites. Instead of 13 brittle
   scrapers, one **generic fetch ladder**: `robots.txt` → `sitemap.xml` → structured feed
   (many broker sites publish their whole inventory as JSON) → HTML + a single LLM
   extraction prompt. No per-site CSS parsers, so a redesign costs nothing and a new site
   is one line of YAML. Plus free government supply — NYC publishes a **vacancy flag** on
   every storefront in the city, which is a lead source no broker feed has.
2. **Enrichment.** Per-metro parcel data (all four keyless), plus Walk Score and Transit
   Score computed from **Walk Score's own published methodology** — it reproduces their
   published values (Empire State Building = 100, Bay Ridge = 98) and shows the
   per-category breakdown, so it explains the score instead of asserting it.
3. **Search.** Plain English → LLM parse → **hard** SQL filter (a constraint is a
   constraint, never ranked away) → FTS5 BM25, fused with optional semantic similarity via
   Reciprocal Rank Fusion → conversational reply.
4. **Workspace.** Per-listing RAG chat, portfolios, saves, CSV/XLSX export, a map.

## Two things it will never do

**It never logs in.** Not to any site, not ever, and there is no flag to make it. Every
scraping case that ended badly ended there.

**It stores facts, not expression.** Address, size, ask, type, broker contact, and a link
back to the original. It does not copy broker marketing prose (descriptions are written
from the facts) and it does not download or re-host their photos.

## It runs with no API keys

Parcel data, walkability, transit, airport drive times, full-text search, the crawler, the
map, portfolios and export are all free and keyless. An Anthropic key unlocks plain-English
search and chat; a Voyage key unlocks semantic ranking. Keys are pasted on a Settings page,
paid calls are capped by a monthly budget, and everything is cached — you never pay for the
same call twice.

## License

PolyForm Noncommercial 1.0.0 — see [`LICENSE.md`](LICENSE.md). Use it for anything except
selling it.
