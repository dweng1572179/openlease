"""config.py's `_drop_inline_comment` validator — explicitly load-bearing (spec/plan): a
`.env` line like `SECRET_KEY=  # note` must not read as a truthy string, or app.py never
generates a random session secret. `python -m app.config` self-checks this manually; this
locks the same behavior as a real pytest so a regression fails the suite.

Run: `python -m pytest tests/test_config.py -v` from openlease/.
"""
from app.config import Settings


def test_inline_comment_on_blank_env_value_is_dropped_not_truthy(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "  # leave blank -> generated")
    s = Settings(_env_file=None)
    assert s.secret_key == "", f"comment leaked into secret_key: {s.secret_key!r}"


def test_inline_comment_guard_applies_to_non_string_fields_too(monkeypatch):
    monkeypatch.setenv("MONTHLY_BUDGET_CENTS", "   # cap raised later")
    s = Settings(_env_file=None)
    assert s.monthly_budget_cents == 1000  # field default, not a crash on int('# cap...')


def test_a_real_value_is_preserved_untouched(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "abc123")
    s = Settings(_env_file=None)
    assert s.voyage_api_key == "abc123"
