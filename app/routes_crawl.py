"""POST /api/crawl — run the ladder over sources.yml. Admin-only (auth-gated), and
long-running, so it reports what it did rather than streaming."""
from fastapi import Depends

from . import crawl
from .app import app, require_auth


@app.post("/api/crawl")
def api_crawl(metro: str | None = None, limit: int = 100, enrich: bool = False,
              _=Depends(require_auth)):
    """Fetch supply. Scoring is a SEPARATE step (`POST /api/enrich`) unless you ask for it
    inline: the free Overpass mirrors rate-limit hard, and scoring in the crawl loop makes
    supply hostage to POI lookups — a measured run spent 30 minutes backing off without
    ever getting past New York."""
    return crawl.run(metro, limit, enrich=enrich)


@app.post("/api/enrich")
def api_enrich(limit: int = 500, _=Depends(require_auth)):
    """Walk/Transit-score every stored listing that has coordinates but no score yet.
    Paced; an Overpass failure leaves the score NULL (never a fake 0)."""
    return {"enriched": crawl.enrich_pending(limit)}


@app.get("/api/crawl/sources")
def api_sources(_=Depends(require_auth)):
    """What we are allowed to fetch, and on which rung. The allowlist IS the scope."""
    return crawl.SOURCES
