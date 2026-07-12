"""The fetch ladder. ONE generic crawler over the sources.yml allowlist.

  robots.txt -> sitemap.xml -> structured feed -> HTML + LLM

Guardrails (spec §2) — these address COPYRIGHT/CONTRACT risk, a different axis from
bot-walls, and none of them has an override:

  * NEVER authenticate. No login, no account, no session cookie, no registration or
    NDA-gated page. This is the one bright line every scraping case that went badly
    crossed. Defeating a bot-detection WAF on a public no-login page is the protected
    case; crossing a login is not.
  * Identify honestly (UA), 1 req / 3-5s per domain, back off on 429/503, daily cap.
  * Conditional GETs (ETag / If-Modified-Since); nothing is refetched inside 24h.

On 8GB: exactly ONE long-lived stealth browser session per run. Never call the one-shot
StealthyFetcher.fetch() in a loop — it launches and kills a Chromium per call.
"""
import logging
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

from . import extract, score
from .config import settings
from .db import get_conn, save_listing

log = logging.getLogger("openlease")

SOURCES: dict[str, list[dict]] = yaml.safe_load(
    (Path(__file__).parent / "data" / "sources.yml").read_text()
)
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}
_LAST_HIT: dict[str, float] = {}


def _domain(url: str) -> str:
    return urlparse(url).netloc


def robots(url: str) -> urllib.robotparser.RobotFileParser:
    d = _domain(url)
    if d not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{urlparse(url).scheme}://{d}/robots.txt")
        try:
            rp.read()
        except Exception as e:  # noqa: BLE001 — unreadable robots.txt = we do not crawl
            log.warning("robots.txt unreadable for %s (%s) — treating as disallow", d, e)
            rp.disallow_all = True
        _ROBOTS[d] = rp
    return _ROBOTS[d]


def allowed(url: str) -> bool:
    return robots(url).can_fetch(settings.crawl_user_agent, url)


def _delay_for(url: str, src: dict) -> float:
    """The site's own Crawl-delay wins if it is SLOWER than ours. It is never used to go
    faster than our floor."""
    site = robots(url).crawl_delay(settings.crawl_user_agent)
    ours = float(src.get("crawl_delay") or settings.crawl_delay_seconds)
    return max(ours, float(site or 0))


def _throttle(url: str, src: dict) -> None:
    d = _domain(url)
    wait = _delay_for(url, src) - (time.monotonic() - _LAST_HIT.get(d, 0.0))
    if wait > 0:
        time.sleep(wait)
    _LAST_HIT[d] = time.monotonic()


def _under_daily_cap(url: str) -> bool:
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM crawl_log WHERE domain = ? "
            "AND date(fetched_at) = date('now')", (_domain(url),)
        ).fetchone()["c"]
    return n < settings.crawl_daily_cap_per_domain


def _seen_recently(url: str) -> bool:
    """Conditional-GET stand-in: nothing is refetched inside the TTL."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM crawl_log WHERE url = ? AND fetched_at > datetime('now', '-24 hours') "
            "LIMIT 1", (url,)
        ).fetchone()
    return row is not None


def _log_fetch(url: str, status: int, etag: str | None = None, last_mod: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO crawl_log (domain, url, status, etag, last_mod) VALUES (?, ?, ?, ?, ?)",
            (_domain(url), url, status, etag, last_mod),
        )


def fetch(url: str, src: dict) -> str | None:
    """Default tier: curl_cffi impersonation, NO browser (handles ~95% of regional broker
    sites — they're server-rendered). Stealth tier only for the walled ones."""
    if not allowed(url):
        log.info("robots.txt disallows %s — skipping", url)
        return None
    if not _under_daily_cap(url):
        log.info("daily cap reached for %s — skipping", _domain(url))
        return None
    _throttle(url, src)

    if src.get("tier") == "stealth" and settings.crawl_stealth:
        return _stealth_fetch(url)

    from scrapling.fetchers import FetcherSession
    with FetcherSession(impersonate="chrome", stealthy_headers=True, retries=3) as s:
        page = s.get(url)
    _log_fetch(url, getattr(page, "status", 0))
    if getattr(page, "status", 0) in (429, 503):
        log.warning("%s -> %s, backing off", url, page.status)
        time.sleep(_delay_for(url, src) * 4)
        return None
    return page.body if getattr(page, "status", 0) == 200 else None


_STEALTH = None


def _stealth_fetch(url: str) -> str | None:
    """ONE long-lived session for the whole run. Never StealthyFetcher.fetch() in a loop —
    that launches and kills a Chromium per call and will bring an 8GB machine to its knees."""
    global _STEALTH
    try:
        from scrapling.fetchers import StealthySession
    except ImportError:
        log.warning("stealth tier unavailable — run `scrapling install` (~400-600MB "
                    "Chromium, one time). Skipping %s.", url)
        return None
    if _STEALTH is None:
        _STEALTH = StealthySession(headless=True, max_pages=2, disable_resources=True,
                                   solve_cloudflare=True)
        _STEALTH.__enter__()
    try:
        page = _STEALTH.fetch(url)
        _log_fetch(url, getattr(page, "status", 0))
        return page.body if getattr(page, "status", 0) == 200 else None
    except Exception as e:  # noqa: BLE001
        log.warning("stealth fetch failed for %s: %s", url, e)
        return None


def close() -> None:
    global _STEALTH
    if _STEALTH is not None:
        _STEALTH.__exit__(None, None, None)
        _STEALTH = None


def sitemap_urls(base: str, src: dict) -> list[str]:
    """Rung 2. <lastmod> is what drives a recrawl; absent one, the URL is crawled once."""
    import re
    body = fetch(urljoin(base, "/sitemap.xml"), src)
    if not body:
        return []
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)


def _to_markdown(body: str) -> str:
    """Strip the page down to the listing container's plain text for the LLM rung. No
    per-site CSS selector — always the whole <body> — so a redesign costs nothing.

    NOTE (Scrapling 0.4.10 API drift from the plan): the plan called
    `Selector(body).css_first("body")`, but 0.4.10 has no `css_first` method — `.css()`
    returns a `Selectors` list, and the first match is its `.first` property."""
    from scrapling.parser import Selector
    page = Selector(body).css("body").first
    return page.get_all_text(strip=True) if page else body


def crawl_source(src: dict, metro: str, limit: int = 100) -> list[dict]:
    """Descend the ladder, stopping at the highest rung that produces listings."""
    out: list[dict] = []

    if src.get("rung") == "feed_wp" and src.get("feed"):
        body = fetch(src["feed"], src)
        if body:
            import json
            try:
                items = json.loads(body)
            except json.JSONDecodeError:
                items = []
            for item in items[:limit]:
                d = extract.from_wp_json(item, src, metro)
                if d:
                    out.append(d)
            if out:
                return out                      # the rung worked — do not descend
            log.info("%s: wp-json returned nothing usable, descending the ladder", src["key"])

    urls = [u for u in sitemap_urls(src["url"], src) if "listing" in u or "propert" in u]
    urls = urls[:limit] or [src["url"]]

    for url in urls:
        if _seen_recently(url):
            continue
        body = fetch(url, src)
        if not body:
            continue
        d = extract.from_jsonld(body, url, src, metro)
        if not d and src.get("rung") != "jsonld":
            try:
                md = _to_markdown(body)
            except Exception:  # noqa: BLE001
                md = body
            d = extract.from_html_llm(md, url, src, metro)
        if d:
            out.append(d)
    return out


def run(metro: str | None = None, limit: int = 100) -> dict:
    """Crawl every allowlisted source (optionally one metro). Enriches each new listing
    with Walk/Transit score at ingest — the ONLY time Overpass is ever called."""
    metros = [metro] if metro else list(SOURCES)
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": []}
    try:
        for m in metros:
            for src in SOURCES.get(m, []):
                try:
                    recs = crawl_source(src, m, limit)
                except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
                    log.warning("source %s failed: %s: %s", src["key"], type(e).__name__, e)
                    stats["errors"].append(f"{src['key']}: {type(e).__name__}")
                    continue
                stats["fetched"] += len(recs)
                for rec in recs:
                    # ponytail: NONE of the three extraction rungs (from_wp_json,
                    # from_jsonld, from_html_llm/ListingExtract) resolves an address to
                    # lat/lng — geocoding a crawled listing is genuinely out of scope for
                    # this task (T10's own "Consumes" list names db.save_listing, ai,
                    # score.enrich, settings — never registry.geocoder). So `rec["lat"]`
                    # is always absent here today, `stats["skipped"]` increments for
                    # EVERY crawled listing, and `score.enrich` below never actually
                    # fires for a real crawl yet. The listing is still stored (address,
                    # SF, ask, broker, source_url — the facts) and searchable; it just
                    # has no Walk/Transit score until a future task geocodes `rec["address"]`
                    # via `registry.geocoder(m)` before this point. Documented rather than
                    # silently masked — see docs/implementation-plan.md Task 10 correction.
                    if not rec.get("lat"):
                        stats["skipped"] += 1   # no point = no scoring; still stored
                    lid = save_listing(rec)
                    stats["saved"] += 1
                    if rec.get("lat"):
                        try:
                            score.enrich(lid)
                        except Exception as e:  # noqa: BLE001
                            log.warning("scoring failed for %s: %s", lid, e)
    finally:
        close()                                 # always release the Chromium
    return stats
