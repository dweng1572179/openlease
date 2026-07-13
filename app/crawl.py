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
import re
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

from . import extract, registry, score
from .config import settings
from .db import get_conn, save_listing
from .models import METROS

log = logging.getLogger("openlease")

SOURCES: dict[str, list[dict]] = yaml.safe_load(
    (Path(__file__).parent / "data" / "sources.yml").read_text()
)
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}
_LAST_HIT: dict[str, float] = {}
_BACKOFF_STREAK: dict[str, int] = {}   # domain -> consecutive 429/503 count


def _domain(url: str) -> str:
    return urlparse(url).netloc


def _get_robots_txt(robots_url: str) -> tuple[int, str]:
    """Fetch robots.txt with OUR honest UA, over the same stack we fetch pages with.

    NOT `RobotFileParser.read()`. That calls urllib.request.urlopen, which sends
    `Python-urllib/3.11` — and broker-site WAFs 403 that UA on sight. RobotFileParser
    treats a 403 as `disallow_all = True`, so we were silently self-blocking on sites
    whose robots.txt actually WELCOMES us: rexfordindustrial.com says `Disallow:` (empty
    = allow all), avisonyoung.us has no Disallow at all, and metro-manhattan.com only
    disallows /blog/ paths. All three came back "disallowed" and we skipped them, which
    zeroed out LA and Chicago entirely.

    That is not obeying robots.txt — it is obeying a WAF's opinion of a User-Agent we
    should never have been sending. We identify honestly, so we ask for robots.txt as
    OpenLeaseBot, the same identity we then check permissions under.
    """
    import httpx
    r = httpx.get(robots_url, headers={"User-Agent": settings.crawl_user_agent},
                  timeout=20.0, follow_redirects=True)
    return r.status_code, r.text


def robots(url: str) -> urllib.robotparser.RobotFileParser:
    d = _domain(url)
    if d not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{d}/robots.txt"
        rp.set_url(robots_url)
        try:
            status, text = _get_robots_txt(robots_url)
            if status in (401, 403):
                # A real refusal, addressed to US, by name. Fail closed.
                log.warning("robots.txt for %s returned %s to our own UA — treating as "
                            "disallow", d, status)
                rp.disallow_all = True
            elif 400 <= status < 500:
                # No robots.txt (404 etc). The standard reading: nothing is forbidden.
                rp.allow_all = True
            elif status >= 500:
                # The site is broken, not refusing us. Don't hammer it.
                log.warning("robots.txt for %s returned %s — treating as disallow", d, status)
                rp.disallow_all = True
            else:
                rp.parse(text.splitlines())
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


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
# A URL that smells like inventory. Deliberately broader than "listing|propert": real sites
# use /space/, /availability/, /building/, /asset/. Generic — never a per-site pattern.
INVENTORY_RE = re.compile(r"propert|listing|space|availab|building|asset", re.I)
# ...but not the brochure. rtl-re.com's sitemap lists a PDF next to every listing, under the
# same /properties/ path, so the inventory pattern matched them and the crawler spent a
# 4-second politeness delay each on 25 PDFs it could never extract a thing from. We only
# ever want the HTML page.
NOT_A_PAGE_RE = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|docx?|xlsx?|pptx?|zip|mp4)$", re.I)
MAX_SITEMAP_CHILDREN = 12


def is_listing_page(url: str) -> bool:
    return bool(INVENTORY_RE.search(url)) and not NOT_A_PAGE_RE.search(url.split("?")[0])


def sitemap_urls(base: str, src: dict) -> list[str]:
    """Rung 2. <lastmod> is what drives a recrawl; absent one, the URL is crawled once.

    Follows a <sitemapindex> into its children. Most of these sites don't publish a flat
    /sitemap.xml — they publish an INDEX whose <loc>s are themselves .xml sitemaps, and
    reading only the top level returned a handful of section URLs and zero listings. That
    is why LA and Chicago looked empty: rexfordindustrial.com has 793 inventory URLs, all
    of them one level down. Also tries /sitemap_index.xml, which is what Yoast (on most of
    these WordPress sites) actually generates.
    """
    body = None
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        body = fetch(urljoin(base, path), src)
        if body:
            break
    if not body:
        return []

    locs = _LOC.findall(body)
    # A sitemap lives at the DOMAIN root, so a source scoped to a section of a site
    # (avisonyoung.us/web/los-angeles/properties-for-lease) pulls that firm's NATIONAL
    # inventory. Keep only what sits under the source's own path — the rest is another
    # market's, and correctness would otherwise rest entirely on the out-of-market guard.
    # Scope to the source path's PARENT — "/web/los-angeles/properties-for-lease" is a page,
    # and the listings under it live at "/web/los-angeles/properties/…". The parent is the
    # market; the leaf is just the index page we were pointed at.
    parts = [p for p in urlparse(base).path.split("/") if p]
    scope = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""
    if scope:
        scoped = [u for u in locs
                  if u.endswith(".xml") or urlparse(u).path.startswith(scope)]
        if scoped:
            locs = scoped
    children = [u for u in locs if u.endswith(".xml")]
    if children:
        out = [u for u in locs if not u.endswith(".xml")]
        for child in children[:MAX_SITEMAP_CHILDREN]:
            sub = fetch(child, src)
            if sub:
                out += [u for u in _LOC.findall(sub) if not u.endswith(".xml")]
        if len(children) > MAX_SITEMAP_CHILDREN:
            log.info("%s: sitemap index has %d children, read the first %d",
                     src.get("key"), len(children), MAX_SITEMAP_CHILDREN)
        return out
    return locs


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
    from .providers import census

    g = None
    try:
        if metro == "nyc":
            from .providers import geosearch
            g = geosearch.geocode(address)
        else:
            prov = registry.parcel_provider(metro)
            g = prov.geocode(address) if prov and hasattr(prov, "geocode") else None
    except Exception as e:  # noqa: BLE001 — a geocoder outage must not crash the crawl
        log.warning("geocoding failed for %r in %s (%s): %s",
                    address, metro, type(e).__name__, e)

    if g:
        return (g["lat"], g["lng"])

    # The metro's own layer came up empty. Fall back to the US Census geocoder — free,
    # keyless, national, government-run.
    #
    # These per-metro layers are PARCEL CACHES, not geocoders. LA County's resolved 2 of 6
    # real Los Angeles addresses; it simply does not contain "540 Rose Avenue, Venice". The
    # crawled LA corpus got 4 map pins out of 74 listings. Census finds all of them.
    try:
        c = census.geocode(address)
        if c:
            return (c["lat"], c["lng"])
    except Exception as e:  # noqa: BLE001
        log.warning("census geocoding failed for %r (%s): %s", address, type(e).__name__, e)
    return None


# The state each metro lives in. Used to reject a national feed's out-of-market listings
# BEFORE we geocode them — see `_out_of_market`.
METRO_STATE = {"nyc": "ny", "mia": "fl", "la": "ca", "chi": "il"}


def metro_for(lat: float, lng: float) -> str | None:
    """Which of our four metros actually contains this point? None = none of them."""
    for key, meta in METROS.items():
        min_lat, min_lng, max_lat, max_lng = meta["bbox"]
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return key
    return None


def _out_of_market(d: dict, metro: str) -> bool:
    """Reject a listing whose own URL says it is in another state — BEFORE geocoding it.

    A bbox check after geocoding cannot catch this, because a metro-scoped geocoder does
    not decline: NYC GeoSearch, handed "302 south colonial drive cleburne TX", returns
    coordinates in BROOKLYN. The wrong answer is already inside the bbox by the time
    `_place` sees it. RIPCO's feed is national, and this is the only signal that says so.
    """
    st = d.get("geo_state")
    if st and st != METRO_STATE.get(metro):
        log.info("%s: %r is in %s, not %s — dropping (out of market)",
                 d.get("source"), d.get("address"), st.upper(), metro)
        return True
    return False


def _maybe_geocode(d: dict) -> None:
    """None of the three extraction rungs (`from_wp_json`/`from_jsonld`/`from_html_llm`)
    resolves an address to lat/lng on its own — this is Task 10's own crawl-time geocode
    step, wired in right after extraction and before the listing is saved. A geocode
    failure leaves lat/lng ABSENT: the listing still saves (address, SF, ask, broker,
    source_url — the facts), it just has no map pin and no Walk Score yet."""
    # Prefer the slug-derived full address ("2446 broadway new york ny") over the post
    # title ("2446 Broadway"): a bare street name has no city, and a metro-scoped geocoder
    # will find a same-named street in its own city and answer confidently. See
    # extract._slug_address.
    addr = d.get("geo_hint") or d.get("address")
    if not addr or d.get("lat"):
        return
    coords = _geocode(addr, d["metro"])
    if coords:
        d["lat"], d["lng"] = coords
    else:
        log.warning("no geocode match for %r in %s (%s) — saving without a map pin",
                    addr, d["metro"], d.get("source"))


def _place(d: dict, configured_metro: str) -> bool:
    """Decide which metro a listing is ACTUALLY in, from its coordinates. Returns False if
    it isn't in any of our four — the caller drops it.

    A source's position in sources.yml is a hint about where to LOOK, not a fact about
    what it returns. RIPCO's wp-json feed is NATIONAL: crawling it under `nyc` stamped
    listings in Tampa, Cleburne TX, and central NJ as New York. And because `ripco` and
    `ripco_mia` are the same site with the same source_urls, the Miami pass then upserted
    over the New York rows and relabelled them `mia` — one feed, two config entries, and
    the metro column ended up meaning nothing.

    The geocoder already told us where the building is. Believe it over the config. A
    listing we cannot place stays unplaced — we do not guess a metro for it.
    """
    lat, lng = d.get("lat"), d.get("lng")
    if lat is None or lng is None:
        return True                      # ungeocoded: keep the configured metro, no pin
    actual = metro_for(lat, lng)
    if actual is None:
        log.info("%s: %r is outside all four metros (%.4f, %.4f) — dropping",
                 d.get("source"), d.get("address"), lat, lng)
        return False
    if actual != configured_metro:
        log.info("%s: %r is in %s, not the configured %s — filing it under %s",
                 d.get("source"), d.get("address"), actual, configured_metro, actual)
    d["metro"] = actual
    return True


# geo_hint and geo_state are HERE on purpose. They are what the out-of-market guard and the
# geocoder run on, and leaving them out of the merge threw them away on exactly the two rungs
# that need them most: when from_html_facts read "Oxnard, CA" off a Rexford page, a listing
# that had arrived via the feed or via JSON-LD lost the city and the state, `_out_of_market`
# saw no state and could not reject it, and the geocoder was handed a bare street name — the
# precise input the whole fix exists to avoid.
# (Neither key is in db._LISTING_COLS, so neither reaches the database.)
_FACT_KEYS = ("size_sf", "asking_rent", "rent_unit", "property_type", "sale_price",
              "transaction_type", "divisible_min_sf", "divisible_max_sf",
              "geo_hint", "geo_state")


def _fill_facts_from_detail_pages(recs: list[dict], src: dict) -> None:
    """A WordPress feed gives the address and the link — and almost never the SIZE or the
    ASK. Those live on the listing's own page.

    Without this the feed rung produced a link directory for the two metros that depend on
    it most (NYC is the primary market): 59 New York listings, 4 of them with a size, none
    with a rent — invisible to every query that says "~1,500 SF under $8k/mo", which is to
    say every real query. The detail page is one more fetch per listing, at the same
    politeness delay as everything else, and it is what makes the row worth having.

    Only ADDS facts; never overwrites what the feed already stated. The feed is the more
    reliable source when it says anything at all.
    """
    for d in recs:
        url = d.get("source_url")
        if not url or all(d.get(k) for k in ("size_sf", "asking_rent")):
            continue
        body = fetch(url, src)
        if not body:
            continue
        facts = extract.from_html_facts(body, url, src, d["metro"])
        if not facts:
            continue
        added = {k: facts[k] for k in _FACT_KEYS if facts.get(k) and not d.get(k)}
        if added:
            d.update(added)
            d["our_description"] = extract.describe(d)   # re-say it now that we know more

        # The detail page may be the first thing that told us WHERE this is (the feed's slug
        # doesn't always carry a state). If so, try again — and if it turns out to be in
        # another market, mark it: the caller drops it.
        if "geo_hint" in added and d.get("lat") is None:
            _maybe_geocode(d)
        if _out_of_market(d, d["metro"]) or (d.get("lat") is not None
                                             and not _place(d, d["metro"])):
            d["_drop"] = True


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
                if d and not _out_of_market(d, metro):   # check BEFORE we geocode
                    _maybe_geocode(d)
                    if _place(d, metro):
                        out.append(d)
            if out:
                _fill_facts_from_detail_pages(out, src)
                # the detail page can reveal a listing is out of market (a feed slug does
                # not always carry a state) — drop those rather than file them here
                out = [d for d in out if not d.pop("_drop", False)]
                return out                      # the rung worked — do not descend
            log.info("%s: wp-json returned nothing usable, descending the ladder", src["key"])

    urls = [u for u in sitemap_urls(src["url"], src) if is_listing_page(u)]
    urls = urls[:limit] or [src["url"]]

    for url in urls:
        if _seen_recently(url):
            continue
        body = fetch(url, src)
        if not body:
            continue
        # Descend only as far as we have to: JSON-LD -> facts-from-text -> the LLM.
        d = extract.from_jsonld(body, url, src, metro)
        if d and not (d.get("size_sf") or d.get("asking_rent")):
            # A rung that "worked" but produced no SIZE and no ASK has not actually given
            # us a listing — the hard filter runs on exactly those two fields, so the row
            # is invisible to every query that matters. Most real-estate JSON-LD is Yoast
            # SEO boilerplate with an address and nothing else, which is why Miami came
            # back with 14 listings and zero sizes. Take the address it found, and go get
            # the facts from the page's own text.
            facts = extract.from_html_facts(body, url, src, metro)
            if facts:
                d.update({k: v for k, v in facts.items()
                          if k in _FACT_KEYS and v and not d.get(k)})
                d["our_description"] = extract.describe(d)
        if not d:
            # Rung 3c, keyless. Most of these sites publish no feed and no real-estate
            # JSON-LD, so without this the ONLY way to get a size or a rent was the paid
            # rung — and a keyless crawl produced addresses with no SF and no ask, which
            # the hard filter cannot filter on. This is what makes the corpus searchable.
            d = extract.from_html_facts(body, url, src, metro)
        if not d and src.get("rung") == "html":
            try:
                md = _to_markdown(body)
            except Exception:  # noqa: BLE001
                md = body
            d = extract.from_html_llm(md, url, src, metro)     # no-op without a key
        if d and not _out_of_market(d, metro):
            _maybe_geocode(d)
            if _place(d, metro):
                out.append(d)
    return out


def run(metro: str | None = None, limit: int = 100, enrich: bool = False) -> dict:
    """Crawl every allowlisted source (optionally one metro), geocoding each listing.

    Enrichment (Walk/Transit score, i.e. Overpass) is DECOUPLED and off by default — call
    `enrich_pending()` afterwards. Both are ingest-time; they just must not share a loop.

    Scoring inline made supply hostage to POI lookups: the free Overpass mirrors soft
    rate-limit hard under a bulk run, and with retry-and-backoff every listing was costing
    8-56s of sleep. A measured run spent 30 minutes and 24 backoffs without ever getting
    past New York — the crawl was throttled to Overpass's pace even though it had nothing
    to ask Overpass for. Fetch supply fast; score it separately, at whatever pace the
    mirror will bear.
    """
    metros = [metro] if metro else list(SOURCES)
    stats: dict = {"fetched": 0, "saved": 0, "no_pin": 0, "per_source": {}, "errors": []}
    try:
        for m in metros:
            for src in SOURCES.get(m, []):
                try:
                    recs = crawl_source(src, m, limit)
                except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
                    log.warning("source %s failed: %s: %s", src["key"], type(e).__name__, e)
                    stats["errors"].append(f"{src['key']}: {type(e).__name__}: {e}"[:120])
                    continue
                stats["fetched"] += len(recs)
                for rec in recs:
                    if not rec.get("lat"):
                        stats["no_pin"] += 1    # no point = no map pin, no score; still stored
                    try:
                        save_listing(rec)
                        stats["saved"] += 1
                    except Exception as e:  # noqa: BLE001 — one bad row must not kill the run
                        # It used to count every attempt as a save, so a run that stored
                        # nothing still reported a full tally.
                        log.warning("could not save %r (%s): %s",
                                    rec.get("source_url"), type(e).__name__, e)
                stats["per_source"][src["key"]] = {
                    "rung": src.get("rung"), "listings": len(recs),
                }
                log.info("%s (%s): %d listings", src["key"], src.get("rung"), len(recs))
    finally:
        close()                                 # always release the Chromium
    if enrich:
        stats["enriched"] = enrich_pending()
    return stats


def enrich_pending(limit: int = 500) -> int:
    """Score every stored listing that has coordinates but no Walk Score yet, and — if a
    Voyage key is set — backfill its embedding.

    Separate from the crawl on purpose (see `run`). Paced by
    `settings.overpass_pace_seconds`, and an Overpass failure is a LOUD skip — the score
    stays NULL, which the UI renders as "not computed". It is never a 0: a 0 is a real
    Walk Score (car-dependent), and a corpus of fake zeros is worse than an empty column.
    """
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM listing WHERE lat IS NOT NULL AND walk_score IS NULL LIMIT ?",
            (limit,)).fetchall()]
    done = 0
    for lid in ids:
        try:
            score.enrich(lid)
            done += 1
        except Exception as e:  # noqa: BLE001
            log.warning("scoring failed for listing %s: %s: %s", lid, type(e).__name__, e)
        time.sleep(settings.overpass_pace_seconds)
    log.info("enriched %d/%d pending listings", done, len(ids))
    embed_pending()
    return done


def embed_pending(limit: int = 2000) -> int:
    """Backfill Voyage embeddings for stored listings. A no-op without a key.

    Nothing in the app called `rank.embed_listings` — it existed, it was tested, and it was
    unreachable. So `listing_vec` was always empty, `cosine_ids` short-circuited on every
    search, and setting VOYAGE_API_KEY changed NOTHING about the results while the Settings
    dashboard cheerfully reported semantic ranking as "on". A feature that is advertised and
    does not run is worse than one that is absent.
    """
    from . import rank
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM listing LIMIT ?", (limit,)).fetchall()]
    n = rank.embed_listings(ids)      # returns 0 with no key; degrades loudly on failure
    if n:
        log.info("embedded %d listings for semantic ranking", n)
    return n
