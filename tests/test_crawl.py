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

    def _fake_run(metro=None, limit=100):
        calls["metro"], calls["limit"] = metro, limit
        return {"fetched": 0, "saved": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr(crawl, "run", _fake_run)
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        r = c.post("/api/crawl", params={"metro": "nyc", "limit": 5})
        assert r.status_code == 200
        assert r.json() == {"fetched": 0, "saved": 0, "skipped": 0, "errors": []}
        assert calls == {"metro": "nyc", "limit": 5}


def test_api_sources_returns_the_allowlist_when_authed():
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        r = c.get("/api/crawl/sources")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"nyc", "mia", "la", "chi"}
        assert any(s["key"] == "ripco" for s in body["nyc"])
