"""settings_store — the spec's core promise: "Keys are pasted in the Settings dashboard,
which overrides .env." (constraints.md). Two behaviors locked down here:

1. `load_overrides()` must make a DB `setting` row win over whatever `.env` already put on
   `settings` — otherwise pasting a key in the dashboard silently does nothing.
2. `save()` must persist + apply without raising, and must actually call
   `registry.reset()` now that Task 7 has landed `app/registry.py` — the guarded import's
   `else:` branch fires live (Task 1 only had the `except ModuleNotFoundError` branch to
   test, since registry.py didn't exist yet; that branch is now unreachable in normal
   operation and is covered instead by test_registry.py's own reset test).

Run: `python -m pytest tests/test_settings_store.py -v` from openlease/.
"""
from app import db, registry, settings_store
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


def test_save_persists_to_db_and_calls_registry_reset(monkeypatch, tmp_path):
    """Now that app/registry.py exists (Task 7), settings_store.save()'s guarded import
    succeeds and its `else:` branch runs `registry.reset()` for real — this is "the whole
    reason the guard was written that way" (task brief). Proven here with a spy instead of
    asserting on registry's actual lru_cache state, so this test stays focused on the
    handoff itself."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "save.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    calls = []
    monkeypatch.setattr(registry, "reset", lambda: calls.append(1))

    settings_store.save({"anthropic_api_key": "sk-pasted-in-dashboard"})  # must not 500

    # the live handoff: registry.reset() was actually invoked, not skipped
    assert calls == [1], "registry.reset() must fire now that app/registry.py exists"

    # applied live
    assert settings.anthropic_api_key == "sk-pasted-in-dashboard"

    # persisted to the DB, not just the in-memory object
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM setting WHERE key = ?", ("anthropic_api_key",)
        ).fetchone()
    assert row["value"] == "sk-pasted-in-dashboard"
