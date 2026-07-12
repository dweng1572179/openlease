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

from . import extract, registry, score
from .config import settings
from .db import get_conn, save_listing

log = logging.getLogger("openlease")

SOURCES: dict[str, list[dict]] = yaml.safe_load(
    (Path(__file__).parent / "data" / "sources.yml").read_text()
)
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}
_LAST_HIT: dict[str, float] = {}
_BACKOFF_STREAK: dict[str, int] = {}   # domain -> consecutive 429/503 count


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


def _backoff(url: str, src: dict, status: int) -> None:
    """EXPONENTIAL (not flat) backoff on 429/503, compounding per CONSECUTIVE failure for
    this domain: base_delay * 2**streak. Resets to 0 the instant this domain answers with
    anything else. Shared by both fetch tiers so `ksr` (sources.yml's own '429-throttles
    aggressively' note — it's `tier: stealth`) gets relief that actually grows, instead of
    the flat 4x multiplier this replaces (which never compounded across repeated hits).
    Capped at streak=6 (~64x the base delay) so one hostile domain can't sleep the whole
    run for hours."""
    d = _domain(url)
    if status not in (429, 503):
        _BACKOFF_STREAK[d] = 0
        return
    _BACKOFF_STREAK[d] = min(_BACKOFF_STREAK.get(d, 0) + 1, 6)
    wait = _delay_for(url, src) * (2 ** _BACKOFF_STREAK[d])
    log.warning("%s -> %s, exponential backoff %.1fs (streak %d)",
                url, status, wait, _BACKOFF_STREAK[d])
    time.sleep(wait)


def _under_daily_cap(url: str) -> bool:
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM crawl_log WHERE domain = ? "
            "AND date(fetched_at) = date('now')", (_domain(url),)
        ).fetchone()["c"]
    return n < settings.crawl_daily_cap_per_domain


def _seen_recently(url: str) -> bool:
    """The TTL half of the conditional-GET story: nothing is even ATTEMPTED inside 24h.
    The other half — ETag/If-Modified-Since for whatever IS fetched after that window —
    is `_conditional_headers()` / `_log_fetch()` below."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM crawl_log WHERE url = ? AND fetched_at > datetime('now', '-24 hours') "
            "LIMIT 1", (url,)
        ).fetchone()
    return row is not None


def _conditional_headers(url: str) -> dict:
    """The most recent ETag/Last-Modified captured for this EXACT url (any past status),
    sent back as If-None-Match / If-Modified-Since so an unchanged page costs the site a
    304 instead of a full re-send. Empty dict (no extra headers) the first time a url is
    ever fetched."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT etag, last_mod FROM crawl_log WHERE url = ? AND (etag IS NOT NULL "
            "OR last_mod IS NOT NULL) ORDER BY fetched_at DESC LIMIT 1", (url,)
        ).fetchone()
    if not row:
        return {}
    headers = {}
    if row["etag"]:
        headers["If-None-Match"] = row["etag"]
    if row["last_mod"]:
        headers["If-Modified-Since"] = row["last_mod"]
    return headers


def _log_fetch(url: str, status: int, etag: str | None = None, last_mod: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO crawl_log (domain, url, status, etag, last_mod) VALUES (?, ?, ?, ?, ?)",
            (_domain(url), url, status, etag, last_mod),
        )


def _decode(page) -> str:
    """scrapling's `Response.body` (BOTH fetch tiers — see `engines/toolbelt/custom.py`,
    `Response.body` is a `@property` returning `self._raw_body`) is BYTES, never `str`.
    Every downstream regex (`sitemap_urls`'s `<loc>` pattern, `extract.from_jsonld`'s
    `_LD`) is str-only, so leaving `fetch()`/`_stealth_fetch()` type-hinted (and
    behaving) as if `.body` were already `str` raised
    `TypeError: cannot use a string pattern on a bytes-like object` on the very first
    successful fetch for every non-`feed_wp` source. Decode HERE, at the one boundary
    where bytes become the crawler's internal currency.

    Respects the response's own detected charset where scrapling exposes it
    (`Response.encoding` — a real attribute, set from the Content-Type header's charset
    in `ResponseFactory`, defaulting to utf-8); falls back to utf-8 with
    errors="replace" so ONE bad byte on a broker page can never kill the crawl."""
    body = page.body
    if isinstance(body, str):        # already str (belt-and-suspenders: a test double,
        return body                  # or a future scrapling version) — nothing to do
    enc = getattr(page, "encoding", None) or "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:              # a charset name scrapling detected but Python's
        return body.decode("utf-8", errors="replace")  # codecs module doesn't know


def fetch(url: str, src: dict) -> str | None:
    """Default tier: curl_cffi impersonation, NO browser (handles ~95% of regional broker
    sites — they're server-rendered). Stealth tier only for the walled ones.

    The UA actually sent on the wire is our own HONEST one (`settings.crawl_user_agent` —
    the identity `robots()`/`allowed()` just checked permissions under): `impersonate=
    "chrome"` still fakes the TLS/JA3 fingerprint and Chrome's `Sec-Ch-Ua` client hints
    (the part that actually helps against a naive bot-wall), but curl_cffi honors an
    explicit `headers=` User-Agent over the one it would otherwise auto-generate for the
    impersonated browser — verified live against the installed curl_cffi (see
    task-10-report.md). Checking robots.txt as OpenLeaseBot and then presenting as Chrome
    on the wire would make the robots check meaningless. The STEALTH tier below is
    different ON PURPOSE — see its own docstring."""
    if not allowed(url):
        log.info("robots.txt disallows %s — skipping", url)
        return None
    if not _under_daily_cap(url):
        log.info("daily cap reached for %s — skipping", _domain(url))
        return None
    _throttle(url, src)

    if src.get("tier") == "stealth" and settings.crawl_stealth:
        return _stealth_fetch(url, src)

    headers = {"User-Agent": settings.crawl_user_agent, **_conditional_headers(url)}
    from scrapling.fetchers import FetcherSession
    with FetcherSession(impersonate="chrome", headers=headers, stealthy_headers=True,
                         retries=3) as s:
        page = s.get(url)
    status = getattr(page, "status", 0)
    resp_headers = getattr(page, "headers", None) or {}
    _log_fetch(url, status, resp_headers.get("etag"), resp_headers.get("last-modified"))
    _backoff(url, src, status)     # sleeps (exponentially) on 429/503; no-op otherwise
    return _decode(page) if status == 200 else None


_STEALTH = None


def _stealth_fetch(url: str, src: dict) -> str | None:
    """ONE long-lived session for the whole run. Never StealthyFetcher.fetch() in a loop —
    that launches and kills a Chromium per call and will bring an 8GB machine to its knees.

    Deliberately DIFFERENT from the default tier above: this tier exists specifically to
    defeat a bot-detection WAF on a PUBLIC, no-login page (constraints.md sanctions this
    explicitly — it's the hiQ-protected case, not the CoStar-v-CREXi one), so full Chrome
    impersonation — headers included — is the point here, not something to correct."""
    global _STEALTH
    try:
        from scrapling.fetchers import StealthySession
    except ImportError:
        log.warning("stealth tier unavailable — run `scrapling install` (~400-600MB "
                    "Chromium, one time). Skipping %s.", url)
        return None
    if _STEALTH is None:
        candidate = StealthySession(headless=True, max_pages=2, disable_resources=True,
                                     solve_cloudflare=True)
        try:
            candidate.__enter__()
        except Exception as e:  # noqa: BLE001 — see the long NOTE below for why "Exception"
            # NOTE: the real failure when `scrapling install` hasn't downloaded Chromium is
            # NOT an ImportError — playwright/patchright import fine as ordinary pip deps
            # (requirements.txt), so that except-clause above never fires for this case.
            # The actual error is raised from INSIDE StealthySession.start() (called by the
            # unguarded `candidate.__enter__()` this used to be), when Playwright/patchright
            # try to launch a Chromium binary that was never downloaded. Verified LIVE
            # against the installed package, Chromium genuinely absent in this environment
            # (see task-10-report.md for the raw transcript):
            #
            #   patchright._impl._errors.Error: BrowserType.launch_persistent_context:
            #   Executable doesn't exist at .../chrome-mac-arm64/Google Chrome for
            #   Testing.app/Contents/MacOS/Google Chrome for Testing
            #   ...Please run the following command to download new browsers:
            #       playwright install
            #
            # Note it's `patchright`'s error class, not `playwright`'s, even though the
            # task brief guessed the latter — `scrapling`'s `StealthySession` launches its
            # browser through `patchright.sync_api`, not vanilla playwright (it only
            # imports plain `playwright` for type hints). Caught broadly here (rather than
            # importing that one private `_impl._errors` class) so ANY start()-time
            # failure degrades the exact same way, never crashes the run.
            log.warning("stealth tier unavailable — run `scrapling install` (~400-600MB "
                        "Chromium, one time) to fix: %s: %s. Skipping %s.",
                        type(e).__name__, e, url)
            return None
        _STEALTH = candidate
    extra_headers = _conditional_headers(url)
    try:
        page = (_STEALTH.fetch(url, extra_headers=extra_headers) if extra_headers
                else _STEALTH.fetch(url))
        status = getattr(page, "status", 0)
        resp_headers = getattr(page, "headers", None) or {}
        _log_fetch(url, status, resp_headers.get("etag"), resp_headers.get("last-modified"))
        _backoff(url, src, status)
        return _decode(page) if status == 200 else None
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


def _geocode(address: str, metro: str) -> tuple[float, float] | None:
    """Resolve an address to (lat, lng) via the METRO'S OWN free, keyless provider — no
    new geocoding dependency, no new API key. NYC's GeoSearch already returns lat/lng
    directly (T7); the other three metros' parcel providers (T9) expose a matching
    `geocode()` that asks their existing ArcGIS/Socrata endpoint for point geometry
    instead of just attributes (added alongside this fix — see parcel_miami.py /
    parcel_la.py / parcel_chicago.py). A failure of ANY kind (no match, the mirror is
    down, a malformed response) returns None — never 0/0 (constraints.md: `None != 0 !=
    "lookup failed"`, and 0,0 is the Gulf of Guinea)."""
    try:
        if metro == "nyc":
            from .providers import geosearch
            g = geosearch.geocode(address)
        else:
            prov = registry.parcel_provider(metro)
            g = prov.geocode(address) if prov and hasattr(prov, "geocode") else None
        return (g["lat"], g["lng"]) if g else None
    except Exception as e:  # noqa: BLE001 — a geocoder outage must not crash the crawl
        log.warning("geocoding failed for %r in %s (%s): %s",
                    address, metro, type(e).__name__, e)
        return None


def _maybe_geocode(d: dict) -> None:
    """None of the three extraction rungs (`from_wp_json`/`from_jsonld`/`from_html_llm`)
    resolves an address to lat/lng on its own — this is Task 10's own crawl-time geocode
    step, wired in right after extraction and before the listing is saved. A geocode
    failure leaves lat/lng ABSENT: the listing still saves (address, SF, ask, broker,
    source_url — the facts), it just has no map pin and no Walk Score yet."""
    addr = d.get("address")
    if not addr or d.get("lat"):
        return
    coords = _geocode(addr, d["metro"])
    if coords:
        d["lat"], d["lng"] = coords
    else:
        log.warning("no geocode match for %r in %s (%s) — saving without a map pin",
                    addr, d["metro"], d.get("source"))


def crawl_source(src: dict, metro: str, limit: int = 100) -> list[dict]:
    """Descend the ladder, stopping at the highest rung that produces listings."""
    out: list[dict] = []

    if src.get("rung") == "feed_wp" and src.get("feed"):
        body = fetch(src["feed"], src)
        if body:
            import json as _json
            try:
                items = _json.loads(body)
            except _json.JSONDecodeError:
                items = []
            for item in items[:limit]:
                d = extract.from_wp_json(item, src, metro)
                if d:
                    _maybe_geocode(d)
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
            _maybe_geocode(d)
            out.append(d)
    return out


def run(metro: str | None = None, limit: int = 100) -> dict:
    """Crawl every allowlisted source (optionally one metro). Geocodes and enriches each
    new listing with Walk/Transit score at ingest — the ONLY time Overpass is ever
    called, and paced (`settings.overpass_pace_seconds` between calls) so a real crawl
    never hammers the one shared free mirror the way Task 8 did doing 12 listings
    back-to-back."""
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
                    if not rec.get("lat"):
                        stats["skipped"] += 1   # no point = no scoring; still stored
                    lid = save_listing(rec)
                    stats["saved"] += 1
                    if rec.get("lat"):
                        try:
                            score.enrich(lid)
                        except Exception as e:  # noqa: BLE001 — an Overpass failure is a
                            # loud skip (score stays null), never a crash and never a 0
                            log.warning("scoring failed for %s: %s", lid, e)
                        time.sleep(settings.overpass_pace_seconds)
    finally:
        close()                                 # always release the Chromium
    return stats
