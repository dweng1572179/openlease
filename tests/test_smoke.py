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
