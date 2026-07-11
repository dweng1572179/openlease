"""settings_store — the spec's core promise: "Keys are pasted in the Settings dashboard,
which overrides .env." (constraints.md). Two behaviors locked down here:

1. `load_overrides()` must make a DB `setting` row win over whatever `.env` already put on
   `settings` — otherwise pasting a key in the dashboard silently does nothing.
2. `save()` must persist + apply without raising even though `app/registry.py` doesn't exist
   until Task 7 (a ModuleNotFoundError there today would turn every `POST /settings` into a
   500 — see review finding #2).

Run: `python -m pytest tests/test_settings_store.py -v` from openlease/.
"""
import logging

from app import db, settings_store
from app.config import settings


def test_db_override_wins_over_env_value(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "overrides.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "from-env")  # what .env set

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?)",
            ("anthropic_api_key", "from-dashboard"),
        )

    settings_store.load_overrides()

    assert settings.anthropic_api_key == "from-dashboard"


def test_save_persists_to_db_and_does_not_raise_without_registry(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "save.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    with caplog.at_level(logging.WARNING, logger="app.settings_store"):
        settings_store.save({"anthropic_api_key": "sk-pasted-in-dashboard"})  # must not 500

    # applied live
    assert settings.anthropic_api_key == "sk-pasted-in-dashboard"

    # persisted to the DB, not just the in-memory object
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM setting WHERE key = ?", ("anthropic_api_key",)
        ).fetchone()
    assert row["value"] == "sk-pasted-in-dashboard"

    # the skip is loud, not silent
    assert any("registry" in r.message and r.levelno == logging.WARNING for r in caplog.records), (
        "expected a WARNING naming the skipped registry.reset(), got: "
        f"{[r.message for r in caplog.records]}"
    )
