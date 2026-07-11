"""End-to-end smoke test: app boots, auth gates, settings renders. Keyless.
Run: `python -m pytest tests/test_smoke.py -v` from openlease/."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_smoke.db")
os.environ["DB_PATH"] = _DB
os.environ["OPENLEASE_PASSWORD"] = "test-pw"
for _k in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "GOOGLE_MAPS_KEY"):
    os.environ[_k] = ""
for _ext in ("", "-wal", "-shm"):  # fresh DB -> the id-1 assumption holds every run
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient  # noqa: E402

from app.app import app  # noqa: E402


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
