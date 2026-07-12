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
    from app import seed
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

        # the listing page renders our prose, links the original, and never re-hosts
        with __import__("app.db", fromlist=["db"]).get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://mia/1'").fetchone()["id"]
        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert "About the property" in r.text and "Wynwood" in r.text
        assert "The broker's own copy and photos stay" in r.text
