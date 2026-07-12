"""End-to-end smoke test: app boots, auth gates, settings renders. Keyless.
Run: `python -m pytest tests/test_smoke.py -v` from openlease/.

Shared env bootstrap (DB_PATH, OPENLEASE_PASSWORD, blank keys) lives in tests/conftest.py —
it has to run before `app.config`'s process-wide `settings` singleton is first created,
which can happen via any test module pytest collects first, not necessarily this one."""
from fastapi.testclient import TestClient

from app.app import app


def test_auth_and_settings():
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login", r.status_code

        assert c.post("/login", data={"password": "nope"}).status_code == 401

        assert c.post("/login", data={"password": "test-pw"}).status_code == 303
        r = c.get("/")
        assert r.status_code == 200 and "Describe the space you need" in r.text

        r = c.get("/settings")
        assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.text, r.text[:300]


def test_search_ui_and_listing_page():
    from app import db, seed
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})

        r = c.get("/")
        assert r.status_code == 200 and 'id="map"' in r.text and "Miami" in r.text

        r = c.post("/search", data={"message": "retail in wynwood around 1500 sf",
                                    "metro": "mia"})
        assert r.status_code == 200, r.text[:300]
        assert "2618 NW 2nd Ave" in r.text
        assert 'id="pins"' in r.text and '"lat": 25.8015' in r.text

        # the listing page renders our prose and never re-hosts. seed data is
        # source_url='seed://...' -- a synthetic marker meaning "no broker page exists" --
        # so the "original listing" link and its footnote must be CORRECTLY ABSENT here.
        # The case where a real http(s) source_url DOES get linked is covered by
        # test_listing_page_links_real_broker_source_url below.
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://mia/1'").fetchone()["id"]
        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert "About the property" in r.text and "Wynwood" in r.text
        assert "original listing" not in r.text
        assert "follow the link above" not in r.text


def test_listing_page_links_real_broker_source_url():
    # The spec's "always link sourceUrl" rule, for the one case that matters for a live
    # crawl: a real http(s) source_url. The link must be rendered AND the footnote must
    # point at it -- unlike the seed:// case above, where both are correctly absent.
    from app import db
    with TestClient(app, follow_redirects=False) as c:
        db.init_db()
        c.post("/login", data={"password": "test-pw"})
        lid = db.save_listing(dict(
            metro="mia", source_url="https://broker.example.com/listings/42",
            address="42 Real Broker Ave, Miami, FL", property_type="retail", size_sf=1000,
            our_description="Ground-floor retail near the broker's own listing page.",
        ))
        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert 'href="https://broker.example.com/listings/42"' in r.text
        assert "original listing" in r.text
        assert "follow the link above" in r.text


def test_new_routes_require_auth():
    # /search, /listings/{id}, /api/listings/{id} all landed in Task 6 -- none may leak a
    # 200 to an unauthenticated caller.
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/search", data={"message": "x", "metro": "nyc"}).status_code != 200
        assert c.get("/listings/1").status_code != 200
        assert c.get("/api/listings/1").status_code != 200
