"""The fetch ladder's safety rails, tested HERMETICALLY — no real network call ever
leaves this process, and no broker site is ever crawled from a test (constraints.md,
"Network in tests"). robots.txt obedience is the one guardrail with NO override flag —
these tests prove `allowed()` actually reads a robots.txt rather than rubber-stamping
every URL, using a robots.txt loaded from canned lines (`RobotFileParser.parse()` takes
lines directly and never touches the network) seeded straight into the module's own
per-domain cache (`crawl._ROBOTS`), never a live fetch.

The rung-by-rung extraction behavior (wp-json / JSON-LD / LLM) is covered by
tests/test_extract.py. This file covers the parts of crawl.py that never touch a broker
site at all: robots obedience, the crawl-delay floor, the daily cap, the recrawl-dedup
log, and that the API route is auth-gated and wires to the ladder correctly.
"""
import logging
import urllib.robotparser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import crawl, db
from app.app import app
from app.config import settings

FAKE_DOMAIN = "test.example.com"
FAKE_URL = f"https://{FAKE_DOMAIN}/listings/1"


def _seed_robots(monkeypatch, lines: list[str]) -> urllib.robotparser.RobotFileParser:
    """Preload crawl._ROBOTS's cache for FAKE_DOMAIN so allowed()/_delay_for() never
    call robots(), and never a network read()."""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(lines)
    monkeypatch.setitem(crawl._ROBOTS, FAKE_DOMAIN, rp)
    return rp


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "crawl.db"))
    db.init_db()


def test_allowed_obeys_robots_disallow(monkeypatch):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow: /wp-admin/"])
    assert crawl.allowed(f"https://{FAKE_DOMAIN}/listings/1") is True
    assert crawl.allowed(f"https://{FAKE_DOMAIN}/wp-admin/") is False, \
        "allowed() must read robots.txt, not rubber-stamp every URL"


def test_delay_for_never_goes_below_our_floor(monkeypatch):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])   # no Crawl-delay stated
    d = crawl._delay_for(FAKE_URL, {"key": "x"})
    assert d >= settings.crawl_delay_seconds


def test_delay_for_honors_a_slower_site_crawl_delay(monkeypatch):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:", "Crawl-delay: 9"])
    d = crawl._delay_for(FAKE_URL, {"key": "x"})
    assert d == 9.0, "the site's own (slower) Crawl-delay must win over our floor"


def test_delay_for_never_speeds_up_even_if_the_site_asks_for_less(monkeypatch):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:", "Crawl-delay: 1"])
    d = crawl._delay_for(FAKE_URL, {"key": "x"})
    assert d == settings.crawl_delay_seconds, \
        "a site claiming a FASTER crawl-delay than our floor must never speed us up"


def test_a_sources_yml_entry_can_override_the_floor_upward(monkeypatch):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    d = crawl._delay_for(FAKE_URL, {"key": "ksr", "crawl_delay": 8})
    assert d == 8.0


def test_fetch_never_touches_the_network_when_robots_disallows(monkeypatch):
    """The one bright line with no override flag: if robots.txt says no, `fetch()` must
    return None before any HTTP call is made — proven by making the fetcher raise if
    it's ever constructed."""
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow: /"])

    def _must_not_be_called(*a, **kw):
        raise AssertionError("fetch() must not touch the network when robots disallows")

    monkeypatch.setattr("scrapling.fetchers.FetcherSession.__init__", _must_not_be_called)
    assert crawl.fetch(FAKE_URL, {"key": "x", "tier": "default"}) is None


def test_under_daily_cap_and_log_fetch(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "crawl_daily_cap_per_domain", 2)
    assert crawl._under_daily_cap(FAKE_URL) is True
    crawl._log_fetch(FAKE_URL, 200)
    crawl._log_fetch(FAKE_URL, 200)
    assert crawl._under_daily_cap(FAKE_URL) is False, \
        "the daily cap must be enforced from the DB (survives a restart), not memory"


def test_seen_recently_dedups_within_the_ttl(isolated_db):
    assert crawl._seen_recently(FAKE_URL) is False
    crawl._log_fetch(FAKE_URL, 200)
    assert crawl._seen_recently(FAKE_URL) is True, \
        "a URL fetched moments ago must not be refetched inside the 24h TTL"


def test_sitemap_urls_reads_lastmod_locs_without_touching_a_real_site(monkeypatch):
    canned = """<?xml version="1.0"?><urlset>
      <url><loc>https://test.example.com/listings/1</loc><lastmod>2026-07-01</lastmod></url>
      <url><loc>https://test.example.com/about</loc></url>
    </urlset>"""
    monkeypatch.setattr(crawl, "fetch", lambda url, src: canned)
    urls = crawl.sitemap_urls("https://test.example.com/", {"key": "x"})
    assert urls == ["https://test.example.com/listings/1", "https://test.example.com/about"]


def test_sources_yml_has_no_login_or_credential_fields():
    """Static guard for the bright line in constraints.md: the allowlist itself may
    never carry a credential-shaped field. Reads the file; makes no network call."""
    raw = (Path(crawl.__file__).parent / "data" / "sources.yml").read_text().lower()
    for banned in ("password:", "login:", "api_key:", "cookie:", "session_token:"):
        assert banned not in raw, f"sources.yml has an auth-shaped field: {banned}"


def test_sources_yml_rungs_are_one_of_the_three_implemented():
    for metro_sources in crawl.SOURCES.values():
        for src in metro_sources:
            assert src["rung"] in ("feed_wp", "jsonld", "html"), src


def test_api_crawl_and_sources_require_auth():
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/api/crawl").status_code != 200
        assert c.get("/api/crawl/sources").status_code != 200


def test_api_crawl_wires_to_the_ladder_when_authed(monkeypatch):
    """Proves the ROUTE calls crawl.run correctly, without ever running crawl.run for
    real (which would fetch every allowlisted broker site — exactly what a test must
    never do)."""
    calls = {}

    def _fake_run(metro=None, limit=100, enrich=False):
        calls["metro"], calls["limit"], calls["enrich"] = metro, limit, enrich
        return {"fetched": 0, "saved": 0, "no_pin": 0, "per_source": {}, "errors": []}

    monkeypatch.setattr(crawl, "run", _fake_run)
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        r = c.post("/api/crawl", params={"metro": "nyc", "limit": 5})
        assert r.status_code == 200
        #  is now  (a listing with no coordinates is stored, just unpinned)
        # and per_source reports which rung each source actually landed on.
        assert r.json() == {"fetched": 0, "saved": 0, "no_pin": 0, "per_source": {}, "errors": []}
        assert calls["enrich"] is False, "scoring is a separate, paced pass — not the crawl loop"
        assert calls == {"metro": "nyc", "limit": 5, "enrich": False}


def test_api_sources_returns_the_allowlist_when_authed():
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        r = c.get("/api/crawl/sources")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"nyc", "mia", "la", "chi"}
        assert any(s["key"] == "ripco" for s in body["nyc"])


# =============================================================================
# Fix pass (review findings) — see task-10-report.md for the full RED/GREEN evidence.
# =============================================================================

class _RawResponse:
    """Mimics scrapling's REAL `Response` shape (`engines/toolbelt/custom.py`): `.body`
    is BYTES, never `str`, for BOTH fetch tiers. Every other fake page object in this
    file (and the ones this bug shipped with) hands `crawl.fetch` a `str` by
    monkeypatching `crawl.fetch` itself — precisely the shortcut that hid Fix 1. This
    fakes the underlying `scrapling.fetchers.FetcherSession`/`StealthySession` instead, so
    `fetch()`'s/`_stealth_fetch()`'s own body-handling code runs against real bytes."""

    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None,
                 encoding: str = "utf-8"):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.encoding = encoding


def _fake_fetcher_session(pages: dict, seen: dict | None = None):
    class _Session:
        def __init__(self, *a, **kw):
            if seen is not None:
                seen["session_kwargs"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            if seen is not None:
                seen["get_kwargs"] = kw
            return pages[url]

    return _Session


# --- Fix 1: fetch() must decode scrapling's real bytes, not return them verbatim -------

def test_fetch_decodes_real_bytes_to_str(monkeypatch, isolated_db):
    """`Response.body` is a `@property` returning `self._raw_body`, which IS bytes for the
    curl_cffi tier — verified against the installed scrapling 0.4.10. The old `fetch()`
    returned `page.body` straight through, type-hinted `str | None`; the very next thing
    that touched it (`sitemap_urls`'s `<loc>` regex) is `str`-only and raised
    `TypeError: cannot use a string pattern on a bytes-like object`."""
    xml = b'<?xml version="1.0"?><urlset><url><loc>https://x/l/1</loc></url></urlset>'
    monkeypatch.setattr("scrapling.fetchers.FetcherSession",
                         _fake_fetcher_session({f"https://{FAKE_DOMAIN}/sitemap.xml": _RawResponse(xml)}))
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])

    body = crawl.fetch(f"https://{FAKE_DOMAIN}/sitemap.xml", {"key": "x", "tier": "default"})

    assert isinstance(body, str), "fetch() must decode bytes -> str, not hand back raw bytes"
    assert body == xml.decode("utf-8")


def test_fetch_falls_back_to_utf8_replace_on_a_bad_byte(monkeypatch, isolated_db):
    """A single malformed byte on a real broker page must degrade that ONE character, not
    kill the crawl (errors="replace", never a raised UnicodeDecodeError)."""
    bad = b'<?xml version="1.0"?><urlset><url><loc>https://x/l/\xff1</loc></url></urlset>'
    monkeypatch.setattr("scrapling.fetchers.FetcherSession",
                         _fake_fetcher_session({f"https://{FAKE_DOMAIN}/sitemap.xml": _RawResponse(bad)}))
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])

    body = crawl.fetch(f"https://{FAKE_DOMAIN}/sitemap.xml", {"key": "x", "tier": "default"})

    assert isinstance(body, str)
    assert "�" in body


def test_ladder_survives_real_bytes_end_to_end_through_jsonld(monkeypatch, isolated_db):
    """The actual failure path the review named: `fetch()` hands `crawl_source()` bytes,
    and BOTH `sitemap_urls()`'s <loc> regex and `extract.from_jsonld()`'s script regex
    are str-only. Feeds the REAL jsonld fixture through as bytes (never a str) at both
    hops and proves a listing comes out, instead of a TypeError three functions deep."""
    detail_bytes = (Path(__file__).parent / "fixtures" / "jsonld_listing.html").read_bytes()
    sitemap_bytes = (b'<?xml version="1.0"?><urlset>'
                      b'<url><loc>https://test.example.com/listing/1</loc></url></urlset>')
    pages = {
        f"https://{FAKE_DOMAIN}/sitemap.xml": _RawResponse(sitemap_bytes),
        "https://test.example.com/listing/1": _RawResponse(detail_bytes),
    }
    monkeypatch.setattr("scrapling.fetchers.FetcherSession", _fake_fetcher_session(pages))
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: None)   # keep this test hermetic
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])

    src = {"key": "test", "name": "Test Brokerage", "url": f"https://{FAKE_DOMAIN}",
           "rung": "jsonld", "tier": "default"}
    recs = crawl.crawl_source(src, "chi", limit=10)

    assert len(recs) == 1
    assert recs[0]["address"] == "1550 N Damen Ave, Wicker Park"


# --- Fix 3: the default tier must send OUR honest UA, not curl_cffi's auto-impersonated one

def test_default_tier_sends_our_honest_user_agent(monkeypatch, isolated_db):
    """robots.txt is checked as `settings.crawl_user_agent` (OpenLeaseBot) — the actual
    fetch must present that SAME identity on the wire. `impersonate="chrome"` may still
    fake the TLS/JA3 fingerprint (that's the point of it); it must not silently generate
    a Chrome User-Agent header that contradicts what robots.txt was evaluated under."""
    seen = {}
    monkeypatch.setattr(
        "scrapling.fetchers.FetcherSession",
        _fake_fetcher_session({f"https://{FAKE_DOMAIN}/page": _RawResponse(b"ok")}, seen),
    )
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])

    crawl.fetch(f"https://{FAKE_DOMAIN}/page", {"key": "x", "tier": "default"})

    headers = seen["session_kwargs"].get("headers") or {}
    assert headers.get("User-Agent") == settings.crawl_user_agent, (
        "the default tier must send OUR honest UA — checking robots.txt as OpenLeaseBot "
        "and then presenting as Chrome on the wire makes the robots check meaningless"
    )


# --- Fix 4: stealth graceful degradation must catch the REAL exception ----------------

def test_stealth_fetch_degrades_gracefully_when_chromium_is_not_installed(monkeypatch, caplog):
    """The real failure when `scrapling install` hasn't downloaded Chromium is raised from
    INSIDE StealthySession.start() (via the unguarded `__enter__()` this replaces) as
    `patchright._impl._errors.Error` ("Executable doesn't exist...") — verified LIVE
    against the installed package with Chromium genuinely absent (see
    task-10-report.md for the raw transcript). The OLD code only caught `ImportError`
    around the (always-succeeding) import, so this exact failure crashed the source
    instead of degrading. Simulated here (never running the real `scrapling install`)."""
    monkeypatch.setattr(crawl, "_STEALTH", None)

    class _BrokenSession:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise RuntimeError(
                "BrowserType.launch_persistent_context: Executable doesn't exist at "
                ".../chrome-mac-arm64/Google Chrome for Testing.app/.../Google Chrome "
                "for Testing\nPlease run: playwright install"
            )

    monkeypatch.setattr("scrapling.fetchers.StealthySession", _BrokenSession)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        result = crawl._stealth_fetch(f"https://{FAKE_DOMAIN}/x", {"key": "ksr", "tier": "stealth"})

    assert result is None, "a broken stealth tier must degrade, never raise, out of fetch()"
    assert any("scrapling install" in r.message for r in caplog.records), \
        "the actionable 'run scrapling install' message never fired"
    assert crawl._STEALTH is None, "a failed __enter__ must not leave a half-initialized session cached"

    # a second attempt (e.g. the next stealth-tier source in the same run) must retry
    # cleanly rather than reuse — or get permanently wedged on — a broken instance
    result2 = crawl._stealth_fetch(f"https://{FAKE_DOMAIN}/y", {"key": "ksr", "tier": "stealth"})
    assert result2 is None


# --- Fix 5: exponential (not flat) backoff on 429/503, both tiers ---------------------

def test_backoff_is_exponential_and_compounds_across_consecutive_429s(monkeypatch):
    monkeypatch.setattr(crawl, "_BACKOFF_STREAK", {})
    sleeps = []
    monkeypatch.setattr(crawl.time, "sleep", lambda s: sleeps.append(s))
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    src = {"key": "x"}
    url = f"https://{FAKE_DOMAIN}/page"

    crawl._backoff(url, src, 429)
    crawl._backoff(url, src, 429)
    crawl._backoff(url, src, 503)

    base = settings.crawl_delay_seconds
    assert sleeps == [base * 2, base * 4, base * 8], (
        "each CONSECUTIVE 429/503 must at least double the previous wait — a flat "
        "multiplier (the old `* 4` every time) never compounds and never actually "
        "backs off a genuinely hostile domain"
    )

    # a real 200 resets the streak
    crawl._backoff(url, src, 200)
    sleeps.clear()
    crawl._backoff(url, src, 429)
    assert sleeps == [base * 2], "a success must reset the streak back to the base delay"


def test_stealth_fetch_also_backs_off_on_429(monkeypatch, isolated_db):
    """The default tier already backed off on 429/503; `_stealth_fetch()` used to just
    silently return None — landing exactly on `ksr`, the one source sources.yml itself
    flags as '429-throttles aggressively' and which is `tier: stealth`."""
    monkeypatch.setattr(crawl, "_STEALTH", None)
    monkeypatch.setattr(crawl, "_BACKOFF_STREAK", {})
    sleeps = []
    monkeypatch.setattr(crawl.time, "sleep", lambda s: sleeps.append(s))
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])

    class _FakeStealthSession:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            return _RawResponse(b"", status=429)

    monkeypatch.setattr("scrapling.fetchers.StealthySession", _FakeStealthSession)

    result = crawl._stealth_fetch(f"https://{FAKE_DOMAIN}/x", {"key": "ksr", "tier": "stealth"})

    assert result is None
    assert sleeps, "the stealth tier must also back off on 429 — it used to return None with no backoff at all"


# --- Fix 6: conditional GETs (ETag / If-Modified-Since), for real ---------------------

def test_conditional_headers_are_sent_after_a_prior_fetch_captured_an_etag(monkeypatch, isolated_db):
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    url = f"https://{FAKE_DOMAIN}/page"
    crawl._log_fetch(url, 200, etag='"abc123"', last_mod="Wed, 21 Oct 2015 07:28:00 GMT")

    seen = {}
    monkeypatch.setattr(
        "scrapling.fetchers.FetcherSession",
        _fake_fetcher_session({url: _RawResponse(b"", status=304)}, seen),
    )

    result = crawl.fetch(url, {"key": "x", "tier": "default"})

    headers = seen["session_kwargs"].get("headers") or {}
    assert headers.get("If-None-Match") == '"abc123"'
    assert headers.get("If-Modified-Since") == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert result is None, "a 304 has no body to extract from — must return None, not crash"


def test_a_fresh_responses_etag_is_captured_for_next_time(monkeypatch, isolated_db):
    """`crawl_log.etag`/`last_mod` used to be dead columns — always inserted as NULL.
    This proves a real response's ETag/Last-Modified headers actually get captured."""
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    url = f"https://{FAKE_DOMAIN}/page"
    resp_headers = {"etag": '"xyz"', "last-modified": "Thu, 01 Jan 1970 00:00:00 GMT"}
    monkeypatch.setattr(
        "scrapling.fetchers.FetcherSession",
        _fake_fetcher_session({url: _RawResponse(b"hello", status=200, headers=resp_headers)}),
    )

    crawl.fetch(url, {"key": "x", "tier": "default"})

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT etag, last_mod FROM crawl_log WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
        ).fetchone()
    assert row["etag"] == '"xyz"'
    assert row["last_mod"] == "Thu, 01 Jan 1970 00:00:00 GMT"


def test_no_prior_fetch_means_no_conditional_headers(monkeypatch, isolated_db):
    """The very first fetch of a URL has nothing to validate against yet."""
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    url = f"https://{FAKE_DOMAIN}/never-seen"
    seen = {}
    monkeypatch.setattr(
        "scrapling.fetchers.FetcherSession",
        _fake_fetcher_session({url: _RawResponse(b"hi")}, seen),
    )

    crawl.fetch(url, {"key": "x", "tier": "default"})

    headers = seen["session_kwargs"].get("headers") or {}
    assert "If-None-Match" not in headers and "If-Modified-Since" not in headers


# --- Fix 7: crawl-time geocoding, via each metro's own keyless provider ---------------

def test_maybe_geocode_sets_lat_lng_via_the_metro_provider(monkeypatch):
    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: (40.75, -73.98))
    d = {"address": "123 Main St", "metro": "nyc"}

    crawl._maybe_geocode(d)

    assert d["lat"] == 40.75 and d["lng"] == -73.98


def test_maybe_geocode_leaves_lat_lng_absent_never_0_0_on_failure(monkeypatch, caplog):
    """constraints.md: `None != 0 != "lookup failed"`, and 0,0 is the Gulf of Guinea. A
    geocode miss must leave lat/lng ABSENT (the listing still saves; it just has no pin
    yet) — never a fabricated (0, 0)."""
    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: None)
    d = {"address": "nowhere real", "metro": "chi", "source": "test"}

    with caplog.at_level(logging.WARNING, logger="openlease"):
        crawl._maybe_geocode(d)

    assert "lat" not in d and "lng" not in d
    assert any("no geocode match" in r.message for r in caplog.records)


def test_geocode_dispatches_nyc_to_geosearch(monkeypatch):
    calls = []

    def _fake_geocode(addr):
        calls.append(addr)
        return {"lat": 1.0, "lng": 2.0}

    monkeypatch.setattr("app.providers.geosearch.geocode", _fake_geocode)

    assert crawl._geocode("100 Main St", "nyc") == (1.0, 2.0)
    assert calls == ["100 Main St"]


def test_geocode_dispatches_other_metros_to_their_parcel_provider(monkeypatch):
    class _FakeProvider:
        def geocode(self, addr):
            return {"lat": 3.0, "lng": 4.0}

    monkeypatch.setattr(crawl.registry, "parcel_provider", lambda metro: _FakeProvider())

    assert crawl._geocode("200 Main St", "mia") == (3.0, 4.0)


def test_geocode_failure_is_caught_and_returns_none_never_crashes(monkeypatch, caplog):
    def _boom(addr):
        raise RuntimeError("mirror is down")

    monkeypatch.setattr("app.providers.geosearch.geocode", _boom)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        result = crawl._geocode("123 Main St", "nyc")

    assert result is None
    assert any("geocoding failed" in r.message for r in caplog.records)


def test_crawl_source_geocodes_each_extracted_record(monkeypatch, isolated_db):
    monkeypatch.setattr(settings, "crawl_delay_seconds", 0.0)
    _seed_robots(monkeypatch, ["User-agent: *", "Disallow:"])
    detail_html = (Path(__file__).parent / "fixtures" / "jsonld_listing.html").read_text()
    sitemap_xml = ('<?xml version="1.0"?><urlset>'
                   '<url><loc>https://test.example.com/listing/1</loc></url></urlset>')
    monkeypatch.setattr(
        crawl, "fetch",
        lambda url, src: sitemap_xml if url.endswith("/sitemap.xml") else detail_html,
    )
    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: (41.9, -87.67))

    src = {"key": "test", "name": "Test Brokerage", "url": f"https://{FAKE_DOMAIN}",
           "rung": "jsonld", "tier": "default"}
    recs = crawl.crawl_source(src, "chi", limit=10)

    assert len(recs) == 1
    assert recs[0]["lat"] == 41.9 and recs[0]["lng"] == -87.67


def test_crawl_does_not_score_inline_and_enrich_pending_paces_overpass(monkeypatch, isolated_db):
    """Scoring inline made SUPPLY hostage to POI lookups: the free Overpass mirrors soft
    rate-limit hard under a bulk run, and with retry-and-backoff each listing cost 8-56s of
    sleep. A measured run burned 30 minutes and 24 backoffs without ever getting past New
    York — throttled to Overpass's pace while it had nothing to ask Overpass for.

    So run() fetches supply and does NOT score. enrich_pending() scores, separately, paced.
    Both are still ingest-time; they just must not share a loop."""
    monkeypatch.setattr(crawl, "SOURCES", {"nyc": [
        {"key": "x", "name": "X", "url": "https://x.example.com", "rung": "jsonld", "tier": "default"},
    ]})
    recs = [
        {"address": "1 Main St", "metro": "nyc", "source": "x",
         "source_url": "https://x.example.com/1", "lat": 40.75, "lng": -73.99},
        {"address": "2 Main St", "metro": "nyc", "source": "x",
         "source_url": "https://x.example.com/2", "lat": 40.76, "lng": -73.98},
    ]
    monkeypatch.setattr(crawl, "crawl_source", lambda src, m, limit: recs)
    monkeypatch.setattr(crawl, "close", lambda: None)
    enrich_calls = []
    monkeypatch.setattr(crawl.score, "enrich",
                        lambda lid, tile_pois=None: enrich_calls.append(lid))
    # Overpass is now asked ONCE PER TILE, not once per listing — count the calls.
    bbox_calls = []
    monkeypatch.setattr(crawl.overpass, "pois_bbox",
                        lambda s, w, n, e: bbox_calls.append((s, w, n, e)) or [{"x": 1}])
    sleeps = []
    monkeypatch.setattr(crawl.time, "sleep", lambda s: sleeps.append(s))

    stats = crawl.run("nyc", limit=5)
    assert stats["saved"] == 2 and stats["no_pin"] == 0
    assert enrich_calls == [], "the crawl must not score inline — that is what throttled it"

    # ...and the separate pass DOES score, and DOES pace itself between Overpass calls
    n = crawl.enrich_pending()
    assert n == 2 and len(enrich_calls) == 2
    assert len(bbox_calls) <= 2, "one Overpass call per TILE, never one per listing"
    assert sleeps, "enrich_pending must still pace itself between Overpass calls"


def test_two_listings_on_the_same_block_share_one_overpass_call(monkeypatch, isolated_db):
    """The point of the tiling. 242 of our listings sit inside Manhattan and Brooklyn, each
    previously pulling its own ~6,000-POI circle that almost entirely overlapped its
    neighbours'. That is 345 requests for about 40 requests' worth of distinct data — it
    took hours, and it made a free public mirror start answering 406 and 504."""
    from app.db import save_listing
    for i in range(8):                                  # 8 listings, one city block apart
        save_listing({"address": f"{i} Main St", "metro": "nyc", "source": "x",
                      "source_url": f"https://x.example.com/{i}",
                      "lat": 40.7488 + i * 0.0002, "lng": -73.9854 + i * 0.0002})
    bbox_calls = []
    monkeypatch.setattr(crawl.overpass, "pois_bbox",
                        lambda s, w, n, e: bbox_calls.append((s, w, n, e)) or [{"x": 1}])
    monkeypatch.setattr(crawl.score, "enrich", lambda lid, tile_pois=None: None)
    monkeypatch.setattr(crawl.time, "sleep", lambda s: None)

    crawl.enrich_pending()
    assert len(bbox_calls) == 1, (
        f"8 listings on one block asked Overpass {len(bbox_calls)} times — they share a tile")


def test_overpass_failure_is_a_loud_skip_never_a_crash_never_a_0(monkeypatch, isolated_db, caplog):
    """An Overpass failure leaves walk_score NULL and says so LOUDLY. Never a crash, and
    never a 0 — a 0 is a REAL Walk Score (car-dependent), and a corpus of fake zeros is
    worse than an empty column."""
    monkeypatch.setattr(crawl, "SOURCES", {"nyc": [
        {"key": "x", "name": "X", "url": "https://x.example.com", "rung": "jsonld", "tier": "default"},
    ]})
    rec = {"address": "1 Main St", "metro": "nyc", "source": "x",
           "source_url": "https://x.example.com/1", "lat": 40.75, "lng": -73.99}
    monkeypatch.setattr(crawl, "crawl_source", lambda src, m, limit: [rec])
    monkeypatch.setattr(crawl, "close", lambda: None)
    monkeypatch.setattr(crawl.time, "sleep", lambda s: None)

    def _boom(lid):
        raise RuntimeError("Overpass is down")

    monkeypatch.setattr(crawl.score, "enrich", _boom)

    crawl.run("nyc", limit=5)
    with caplog.at_level(logging.WARNING, logger="openlease"):
        assert crawl.enrich_pending() == 0        # nothing scored, and it did not raise
    assert "scoring failed" in caplog.text.lower()

    with db.get_conn() as conn:
        row = conn.execute("SELECT walk_score FROM listing WHERE source_url = ?",
                           (rec["source_url"],)).fetchone()
    assert row["walk_score"] is None, "a failed lookup is NULL, never a fabricated 0"

def _robots_response(monkeypatch, status: int, text: str):
    monkeypatch.setattr(crawl, "_get_robots_txt", lambda url: (status, text))
    crawl._ROBOTS.pop("robots.example.com", None)


def test_an_empty_disallow_means_allow_everything(monkeypatch):
    """`Disallow:` with nothing after it is robots.txt for "you may crawl anything" —
    it is what rexfordindustrial.com actually serves. Reading it as a blanket refusal
    silently zeroed out entire metros."""
    _robots_response(monkeypatch, 200, "User-agent: *\nDisallow:\n")
    assert crawl.allowed("https://robots.example.com/properties") is True


def test_a_group_with_no_disallow_at_all_means_allow_everything(monkeypatch):
    """avisonyoung.us serves a User-Agent line and a Sitemap line, and no rules."""
    _robots_response(monkeypatch, 200,
                     "User-Agent: *\nSitemap: https://robots.example.com/sitemap.xml\n")
    assert crawl.allowed("https://robots.example.com/web/chicago/properties-for-lease") is True


def test_we_still_obey_a_real_disallow(monkeypatch):
    """The fix must not turn into 'allow everything'. metro-manhattan.com disallows
    /blog/ paths and nothing else — both halves of that must hold."""
    _robots_response(monkeypatch, 200,
                     "User-agent: *\nDisallow: /blog/feature/\nDisallow: /wp-admin/\n")
    assert crawl.allowed("https://robots.example.com/") is True
    assert crawl.allowed("https://robots.example.com/blog/feature/x") is False
    assert crawl.allowed("https://robots.example.com/wp-admin/") is False


def test_a_403_on_robots_txt_fails_CLOSED(monkeypatch):
    """A refusal addressed to our own honest UA is a real refusal. Fail closed."""
    _robots_response(monkeypatch, 403, "")
    assert crawl.allowed("https://robots.example.com/anything") is False


def test_no_robots_txt_at_all_means_nothing_is_forbidden(monkeypatch):
    """404 = the site published no rules. The standard reading is 'not forbidden',
    NOT 'forbidden'."""
    _robots_response(monkeypatch, 404, "<!doctype html><h1>Not Found</h1>")
    assert crawl.allowed("https://robots.example.com/listings/1") is True


def test_robots_txt_is_requested_under_our_own_user_agent(monkeypatch):
    """The bug: RobotFileParser.read() sends `Python-urllib/3.x`, broker WAFs 403 that on
    sight, and RobotFileParser turns a 403 into disallow_all — so we self-blocked on sites
    that welcomed us. We were not obeying robots.txt; we were obeying a WAF's opinion of a
    UA we should never have sent. Ask as OpenLeaseBot: the same identity we then check
    permissions under."""
    seen = {}

    def _fake_get(url, headers=None, timeout=None, follow_redirects=None):
        seen["url"] = url
        seen["ua"] = (headers or {}).get("User-Agent")

        class _R:
            status_code = 200
            text = "User-agent: *\nDisallow:\n"
        return _R()

    monkeypatch.setattr("httpx.get", _fake_get)
    crawl._ROBOTS.pop("robots.example.com", None)
    assert crawl.allowed("https://robots.example.com/x") is True
    assert seen["url"] == "https://robots.example.com/robots.txt"
    assert seen["ua"] == settings.crawl_user_agent
    assert "OpenLeaseBot" in seen["ua"]


# --- a listing's metro comes from WHERE IT IS, not from which config block found it ------

def test_a_national_feed_does_not_stamp_every_listing_with_the_configured_metro():
    """RIPCO's wp-json feed is national. Crawled under `nyc`, it handed us buildings in
    Tampa and Cleburne, Texas — and we filed them as New York. `_place` believes the
    geocoder over sources.yml: a listing goes in the metro whose bbox actually contains
    it, and a listing outside all four is dropped rather than guessed at."""
    tampa = {"address": "1704 S Dale Mabry Hwy", "lat": 27.9403, "lng": -82.5065,
             "metro": "nyc", "source": "ripco"}
    assert crawl._place(tampa, "nyc") is False          # not one of our four -> dropped

    wynwood = {"address": "2618 NW 2nd Ave", "lat": 25.8010, "lng": -80.1990,
               "metro": "nyc", "source": "ripco"}       # found under the nyc entry...
    assert crawl._place(wynwood, "nyc") is True
    assert wynwood["metro"] == "mia"                    # ...but it is plainly in Miami

    midtown = {"address": "350 5th Ave", "lat": 40.7484, "lng": -73.9857,
               "metro": "nyc", "source": "ripco"}
    assert crawl._place(midtown, "nyc") is True and midtown["metro"] == "nyc"


def test_an_ungeocoded_listing_keeps_its_configured_metro_and_is_not_dropped():
    """No coordinates is not the same as 'somewhere else'. The listing still has an
    address, a source_url and our description — it just has no map pin yet."""
    d = {"address": "somewhere unresolvable", "metro": "chi", "source": "baum"}
    assert crawl._place(d, "chi") is True
    assert d["metro"] == "chi"


def test_sitemap_follows_an_index_into_its_children(monkeypatch):
    """Most of these sites publish a sitemap INDEX, not a flat sitemap. Reading only the
    top level returned a handful of section URLs and zero listings — which is exactly why
    LA and Chicago looked empty (rexfordindustrial.com has 793 inventory URLs, all one
    level down)."""
    index = ('<sitemapindex><sitemap><loc>https://x.test/props-1.xml</loc></sitemap>'
             '<sitemap><loc>https://x.test/posts.xml</loc></sitemap></sitemapindex>')
    children = {
        "https://x.test/props-1.xml":
            "<urlset><url><loc>https://x.test/properties/a</loc></url>"
            "<url><loc>https://x.test/properties/b</loc></url></urlset>",
        "https://x.test/posts.xml":
            "<urlset><url><loc>https://x.test/blog/hello</loc></url></urlset>",
    }

    def _fake_fetch(url, src):
        if url.endswith("/sitemap.xml"):
            return index
        return children.get(url)

    monkeypatch.setattr(crawl, "fetch", _fake_fetch)
    urls = crawl.sitemap_urls("https://x.test", {"key": "x"})
    assert "https://x.test/properties/a" in urls
    assert "https://x.test/properties/b" in urls
    assert not any(u.endswith(".xml") for u in urls)     # children are followed, not returned
    # ...and the inventory filter keeps the properties and drops the blog post
    inv = [u for u in urls if crawl.INVENTORY_RE.search(u)]
    assert sorted(inv) == ["https://x.test/properties/a", "https://x.test/properties/b"]


def test_the_crawler_does_not_fetch_the_brochure_pdf():
    """rtl-re.com's sitemap lists a PDF beside every listing, under the same /properties/
    path — so the inventory pattern matched them and the crawler spent a 4-second
    politeness delay each on 25 PDFs it could never extract anything from."""
    assert crawl.is_listing_page("https://rtl-re.com/properties/604-pacific-street/") is True
    assert crawl.is_listing_page(
        "https://rtl-re.com/properties/604-pacific-street/604-pacific-street.pdf") is False
    assert crawl.is_listing_page("https://x.test/properties/a.jpg") is False
    assert crawl.is_listing_page("https://x.test/blog/hello") is False       # not inventory
    assert crawl.is_listing_page("https://x.test/listings/1?utm=x") is True  # query is fine


def test_a_national_feed_is_filtered_by_state_BEFORE_geocoding():
    """The bbox check alone cannot save us: a metro-scoped geocoder does not decline. NYC
    GeoSearch, handed "302 south colonial drive cleburne TX", returns coordinates in
    BROOKLYN — the wrong answer is already inside the bbox by the time _place sees it. The
    listing's own URL slug names the state, and that is the only signal that says so."""
    tx = {"address": "302 South Colonial Drive", "geo_state": "tx", "source": "ripco"}
    assert crawl._out_of_market(tx, "nyc") is True

    ny = {"address": "2446 Broadway", "geo_state": "ny", "source": "ripco"}
    assert crawl._out_of_market(ny, "nyc") is False

    fl = {"address": "2618 NW 2nd Ave", "geo_state": "fl", "source": "ripco"}
    assert crawl._out_of_market(fl, "mia") is False      # Florida IS Miami's state

    # ...and a Florida listing found on a CHICAGO source is not garbage — it is a MIAMI
    # listing that happens to have turned up somewhere else. Dropping it (which is what
    # this test used to assert) is how RIPCO's entire Florida book got deleted. Re-route
    # it, so it gets Miami's geocoder rather than Chicago's, and let _place confirm by
    # bbox. Texas above is the case that must still be dropped: we don't cover it, so
    # there is no geocoder that would answer honestly.
    fl2 = {"address": "2618 NW 2nd Ave", "geo_state": "fl", "source": "ripco", "metro": "chi"}
    assert crawl._out_of_market(fl2, "chi") is False
    assert fl2["metro"] == "mia"

    unknown = {"address": "somewhere", "source": "x"}    # no slug state -> we don't guess
    assert crawl._out_of_market(unknown, "nyc") is False


def test_the_feed_rung_visits_the_detail_page_for_the_facts(monkeypatch, isolated_db):
    """A WordPress feed gives the address and the link — and almost never the SIZE or the
    ASK. Those are on the listing's own page. Without this the feed rung produced a link
    directory for the two metros that depend on it most (NYC is the primary market): 59
    New York listings, 4 with a size, none with a rent — invisible to every query that says
    "~1,500 SF under $8k/mo", which is every real query."""
    detail = """<html><h1>57 West 38th Street</h1>
                <p>9,470 SF of prime Midtown retail. Incredible flagship opportunity!</p></html>"""
    monkeypatch.setattr(crawl, "fetch", lambda url, src: detail)

    recs = [{"address": "57 West 38th Street", "metro": "nyc", "source": "ripco",
             "source_url": "https://www.ripcony.com/property-listings/57-west-38th-street/",
             "our_description": "Commercial space at 57 West 38th Street."}]
    crawl._fill_facts_from_detail_pages(recs, {"key": "ripco", "name": "RIPCO"})

    assert recs[0]["size_sf"] == 9470            # now the hard filter can see it
    assert recs[0]["property_type"] == "retail"
    # ...and our_description was re-said now that we know more — still OURS, never theirs
    assert "9,470 SF" in recs[0]["our_description"]
    assert "flagship" not in recs[0]["our_description"].lower()


def test_the_detail_pass_never_overwrites_what_the_feed_already_said(monkeypatch, isolated_db):
    """The feed is the more reliable source when it says anything at all.

    The old version of this test gave the record BOTH size and rent, so
    `_fill_facts_from_detail_pages` short-circuited before fetching anything — the merge it
    claimed to test never ran, and it would have made a LIVE network call the day that guard
    changed. Give it a record with a gap, so the merge actually executes."""
    detail = ("<html><h1>1 Main Street</h1><p>Size: 9,999 SF. Asking Rent: $1/SF/yr. "
              "Industrial.</p></html>")
    monkeypatch.setattr(crawl, "fetch", lambda url, src: detail)
    monkeypatch.setattr(crawl, "_maybe_geocode", lambda d: None)

    recs = [{"address": "1 Main St", "metro": "nyc", "source": "x",
             "source_url": "https://x.test/1", "size_sf": 1500}]     # rent is missing
    crawl._fill_facts_from_detail_pages(recs, {"key": "x", "name": "X"})

    assert recs[0]["size_sf"] == 1500, "the feed already said 1,500 — do NOT overwrite it"
    assert recs[0]["asking_rent"] == 1.0, "...but DO fill the gap the feed left"


def test_a_rung_that_finds_no_size_and_no_ask_has_not_found_a_listing(monkeypatch, isolated_db):
    """The hard filter runs on SIZE and RENT. A rung that "worked" but produced neither has
    given us a row invisible to every query that matters. Most real-estate JSON-LD is Yoast
    SEO boilerplate — an address and nothing else — which is why Miami came back with 14
    listings and ZERO sizes. Take the address it found, then go get the facts from the page."""
    page = ("<html><h1>2618 NW 2nd Ave</h1>"
            "<script type=\"application/ld+json\">"
            '{"@type":"Place","address":{"streetAddress":"2618 NW 2nd Ave",'
            '"addressLocality":"Wynwood"}}</script>'
            "<p>1,500 SF of ground-floor retail. $95/SF/yr.</p></html>")
    monkeypatch.setattr(crawl, "fetch", lambda url, src: page)
    monkeypatch.setattr(crawl, "sitemap_urls",
                        lambda base, src: ["https://m1.test/listings/2618-nw-2nd-ave"])
    monkeypatch.setattr(crawl, "_maybe_geocode", lambda d: None)
    monkeypatch.setattr(crawl, "_seen_recently", lambda url: False)

    src = {"key": "metro1", "name": "Metro 1", "url": "https://m1.test", "rung": "jsonld"}
    out = crawl.crawl_source(src, "mia", limit=1)

    assert len(out) == 1
    d = out[0]
    assert d["address"].startswith("2618 NW 2nd Ave")   # JSON-LD gave us this
    assert d["size_sf"] == 1500                          # ...the page's text gave us these
    assert d["asking_rent"] == 95.0
    assert d["property_type"] == "retail"


def test_census_is_the_fallback_when_the_metro_layer_comes_up_empty(monkeypatch, isolated_db):
    """The per-metro layers are PARCEL CACHES, not geocoders. LA County's resolved 2 of 6
    real Los Angeles addresses — it simply does not contain "540 Rose Avenue, Venice" — so
    the crawled LA corpus got 4 map pins out of 74 listings. The US Census geocoder is free,
    keyless, national and government-run, and it finds all of them."""
    from app.providers import census
    monkeypatch.setattr(crawl.registry, "parcel_provider", lambda metro: None)
    monkeypatch.setattr(census, "geocode",
                        lambda addr: {"lat": 33.9986, "lng": -118.4729, "matched": addr})
    assert crawl._geocode("540 Rose Avenue, Venice, CA", "la") == (33.9986, -118.4729)


def test_census_still_refuses_to_guess(monkeypatch, isolated_db):
    from app.providers import census
    monkeypatch.setattr(crawl.registry, "parcel_provider", lambda metro: None)
    monkeypatch.setattr(census, "geocode", lambda addr: None)
    assert crawl._geocode("nowhere at all", "la") is None      # None, never 0/0


def test_the_city_and_state_survive_the_merge_into_the_feed_and_jsonld_rungs():
    """C4. `_FACT_KEYS` didn't carry geo_hint/geo_state, so when from_html_facts read
    "Oxnard, CA" off a Rexford page, a listing that had arrived via the feed or via JSON-LD
    LOST the city and the state: `_out_of_market` saw no state and could not reject it, and
    the geocoder was handed a bare street name — the precise input the whole fix exists to
    avoid. And neither key may reach the DB."""
    from app.db import _LISTING_COLS
    assert "geo_hint" in crawl._FACT_KEYS and "geo_state" in crawl._FACT_KEYS
    assert "geo_hint" not in _LISTING_COLS and "geo_state" not in _LISTING_COLS


def test_a_section_scoped_source_does_not_pull_the_whole_firms_national_sitemap(monkeypatch):
    """M4. A sitemap lives at the DOMAIN root, so a source scoped to a section
    (avisonyoung.us/web/los-angeles/properties-for-lease) pulled Avison Young's NATIONAL
    inventory — every market they operate in. Correctness then rested entirely on the
    out-of-market guard catching it afterwards."""
    sm = ("<urlset>"
          "<url><loc>https://ay.test/web/los-angeles/properties/1</loc></url>"
          "<url><loc>https://ay.test/web/chicago/properties/2</loc></url>"
          "<url><loc>https://ay.test/web/houston/properties/3</loc></url>"
          "</urlset>")
    monkeypatch.setattr(crawl, "fetch", lambda url, src: sm)
    urls = crawl.sitemap_urls("https://ay.test/web/los-angeles/properties-for-lease",
                              {"key": "avison_la"})
    assert urls == ["https://ay.test/web/los-angeles/properties/1"]


def test_the_newsroom_is_not_inventory():
    """INVENTORY_RE is a substring match, so it fires on the editorial section of every
    broker who writes about the market they sell into: 'space' inside
    /articles/creatingwellnessspace-s-, 'building' inside
    /articles/wynwood-nightclub-building-hits-the-market. Metro 1's entire Miami
    'inventory' was three blog posts."""
    for url in ["https://www.metro1.com/articles/creatingwellnessspaces",
                "https://www.metro1.com/articles/wynwood-nightclub-building-hits-the-market",
                "https://www.metro1.com/articles/restaurants-fear-going-from-closed-to-space-for-lease",
                "https://x.com/news/new-listing-in-soho",
                "https://x.com/team/jane-doe", "https://x.com/insights/q3-office-report",
                "https://x.com/properties/brochure.pdf"]:
        assert not crawl.is_listing_page(url), f"the crawler thinks this is inventory: {url}"


def test_real_listing_urls_still_pass():
    for url in ["https://www.rexfordindustrial.com/property/chatsworth-industrial-park/",
                "https://x.com/properties/123-main-st/",
                "https://x.com/listings/450-park-ave",
                "https://x.com/available-space/2618-nw-2nd-ave"]:
        assert crawl.is_listing_page(url), f"the crawler dropped a real listing: {url}"


def test_a_state_we_cover_is_rerouted_not_dropped():
    """RIPCO's feed is NATIONAL (833 listings) and is filed under nyc because that is where
    RIPCO is headquartered, not because that is all it sells. The old guard dropped every
    non-NY row, which threw away the firm's entire Florida book — RIPCO is one of the
    largest RETAIL brokerages in Miami, and Miami is the metro this product demos in. We
    were deleting the answer to our own example query."""
    d = {"address": "20295 NW 2nd Ave", "geo_state": "fl", "metro": "nyc", "source": "ripco"}
    assert crawl._out_of_market(d, "nyc") is False
    assert d["metro"] == "mia", "a Florida listing must be geocoded as Miami, not New York"

    d = {"address": "1234 Sunset Blvd", "geo_state": "ca", "metro": "nyc", "source": "ripco"}
    assert crawl._out_of_market(d, "nyc") is False
    assert d["metro"] == "la"


def test_a_state_we_do_not_cover_is_still_dropped():
    """The guard runs BEFORE the geocode for a reason: NYC GeoSearch, handed
    '302 south colonial drive cleburne TX', returns coordinates in BROOKLYN. The wrong
    answer is already inside the bbox by the time _place sees it."""
    for state in ("tx", "nj", "oh"):
        d = {"address": "302 S Colonial Dr", "geo_state": state, "metro": "nyc"}
        assert crawl._out_of_market(d, "nyc") is True, f"{state} must never reach a geocoder"


def test_the_configured_state_passes_untouched():
    d = {"address": "450 Park Ave", "geo_state": "ny", "metro": "nyc"}
    assert crawl._out_of_market(d, "nyc") is False
    assert d["metro"] == "nyc"


def test_the_feed_rung_reads_every_page_not_just_the_first(monkeypatch):
    """WordPress caps per_page at 100 and says NOTHING about the rest — a feed of 833
    listings hands back 100 and stays silent about the other 733 unless you ask for
    &page=2. We never asked. RIPCO's Miami-Dade inventory lives past page 1, which is why
    routing Florida listings to Miami surfaced only Panama City, Tampa and Sarasota: those
    were the Florida rows that happened to fall in the first 100."""
    import json
    pages = {
        "https://x.example.com/feed": [{"id": i} for i in range(100)],
        "https://x.example.com/feed&page=2": [{"id": i} for i in range(100, 200)],
        "https://x.example.com/feed&page=3": [{"id": i} for i in range(200, 240)],  # short = last
    }
    monkeypatch.setattr(crawl, "fetch",
                        lambda url, src: json.dumps(pages[url]) if url in pages else None)
    src = {"key": "x", "feed": "https://x.example.com/feed"}
    items = crawl._feed_items(src, limit=500)
    assert len(items) == 240, f"read only {len(items)} of 240 — pagination stopped early"
    assert items[-1]["id"] == 239


def test_the_feed_rung_stops_at_the_limit():
    """...but it does not walk 833 listings to satisfy a limit of 10."""
    import json
    fetched = []

    def fake_fetch(url, src):
        fetched.append(url)
        return json.dumps([{"id": i} for i in range(100)])

    import pytest as _p
    with _p.MonkeyPatch().context() as m:
        m.setattr(crawl, "fetch", fake_fetch)
        items = crawl._feed_items({"key": "x", "feed": "https://x.example.com/feed"}, limit=50)
    assert len(fetched) == 1, "walked past the limit"
    assert len(items) == 100


def test_an_address_range_is_collapsed_for_the_geocoder():
    """A multi-tenant industrial building spans a range of street numbers and its page is
    titled with both ends — "1160-1170 N Gilbert Street" — which reaches us as two house
    numbers in a row. No geocoder resolves an address with two house numbers, so 155 of
    Rexford's 320 buildings (half of LA) had no map pin."""
    assert crawl._collapse_range("1160 1170 N Gilbert Street") == "1160 N Gilbert Street"
    assert crawl._collapse_range("18310 18330 Oxnard Street") == "18310 Oxnard Street"
    # a normal address is left alone
    assert crawl._collapse_range("5421 Argosy Avenue") is None
    assert crawl._collapse_range("540 Rose Avenue, Venice, CA") is None
    # ...and a street that BEGINS with a number is not a range ("1 Wall Street")
    assert crawl._collapse_range("1 Wall Street") is None


def test_the_range_retry_runs_inside_geocode_and_never_rewrites_the_address(monkeypatch):
    """Only the geocoder QUERY is normalized. The stored address stays exactly as the broker
    published it, because "1160-1170 N Gilbert" is what the sign on the door says."""
    from app.providers import census

    asked = []

    def fake_census(addr):
        asked.append(addr)
        # the real Census geocoder resolves ONE house number, never a range
        return {"lat": 34.0, "lng": -118.0} if addr == "1160 N Gilbert Street" else None

    monkeypatch.setattr(census, "geocode", fake_census)
    monkeypatch.setattr(crawl.registry, "parcel_provider", lambda m: None)

    d = {"address": "1160 1170 N Gilbert Street", "metro": "la"}
    crawl._maybe_geocode(d)

    assert asked == ["1160 1170 N Gilbert Street", "1160 N Gilbert Street"], \
        f"the range was never retried — asked {asked}"
    assert d["lat"] == 34.0 and d["lng"] == -118.0
    assert d["address"] == "1160 1170 N Gilbert Street", "the crawler rewrote the address"
