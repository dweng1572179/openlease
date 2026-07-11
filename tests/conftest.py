"""Shared test bootstrap for the whole suite.

`app.config.settings` is a process-wide singleton, created once on the first import of
`app.config` — whichever test module pytest happens to collect first (alphabetical by
default: test_cache.py before test_smoke.py). A conftest.py's module-level code always runs
before any test file in its directory is collected, so this is the one place that can
guarantee every test sees the same base settings (a known password, blank provider keys, an
isolated DB path) no matter the collection order.

Per-test isolation on top of this (a scratch DB, a different budget, a different key) goes
through the `monkeypatch` fixture in the individual test files, not here.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_tests.db")
os.environ["DB_PATH"] = _DB
os.environ["OPENLEASE_PASSWORD"] = "test-pw"
for _k in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "GOOGLE_MAPS_KEY"):
    os.environ[_k] = ""
for _ext in ("", "-wal", "-shm"):  # fresh DB -> stable state across runs
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass
