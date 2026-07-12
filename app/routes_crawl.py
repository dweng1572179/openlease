"""POST /api/crawl — run the ladder over sources.yml. Admin-only (auth-gated), and
long-running, so it reports what it did rather than streaming."""
from fastapi import Depends

from . import crawl
from .app import app, require_auth


@app.post("/api/crawl")
def api_crawl(metro: str | None = None, limit: int = 100, _=Depends(require_auth)):
    return crawl.run(metro, limit)


@app.get("/api/crawl/sources")
def api_sources(_=Depends(require_auth)):
    """What we are allowed to fetch, and on which rung. The allowlist IS the scope."""
    return crawl.SOURCES
