"""cache.cached()'s monthly budget guardrail (spec §6, §8) — "never pay twice, never
overspend": a paid MISS is refused once it would push spend over `monthly_budget_cents`,
but a cache HIT is always free and must never be refused, even while already over budget.

Run: `python -m pytest tests/test_cache.py -v` from openlease/.
"""
import pytest

from app import cache, db
from app.cache import BudgetExceeded
from app.config import settings


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """A throwaway DB per test so spend accounting can't bleed between tests."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "cache.db"))
    db.init_db()


def test_paid_miss_accumulates_spend_and_is_blocked_once_over_budget(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "monthly_budget_cents", 100)
    calls = []

    def fetch():
        calls.append(1)
        return {"ok": True}

    # first paid miss: 60c of a 100c cap -> allowed, spend accumulates
    resp = cache.cached("test-provider", "ep", {"q": 1}, fetch, cost_cents=60)
    assert resp == {"ok": True}
    assert cache.spend_this_month() == 60

    # a second, DIFFERENT request: another 60c would put spend at 120c > the 100c cap
    with pytest.raises(BudgetExceeded):
        cache.cached("test-provider", "ep", {"q": 2}, fetch, cost_cents=60)

    # refused means refused: no fetch happened, no spend was recorded for it
    assert len(calls) == 1
    assert cache.spend_this_month() == 60


def test_cache_hit_is_never_refused_even_over_budget(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    calls = []

    def fetch():
        calls.append(1)
        return {"cached": True}

    # prime the cache while the budget is healthy
    cache.cached("test-provider", "ep", {"q": "hit"}, fetch, cost_cents=60)
    assert cache.spend_this_month() == 60

    # now the budget shrinks below what's already spent -- genuinely over budget
    monkeypatch.setattr(settings, "monthly_budget_cents", 10)
    assert cache.budget_remaining_cents() < 0

    # a fresh paid MISS is correctly refused right now...
    with pytest.raises(BudgetExceeded):
        cache.cached("test-provider", "ep", {"q": "brand-new"}, fetch, cost_cents=5)

    # ...but re-requesting the ALREADY-CACHED key must still succeed: you never pay twice,
    # so being over budget must not block a hit.
    resp = cache.cached("test-provider", "ep", {"q": "hit"}, fetch, cost_cents=60)
    assert resp == {"cached": True}
    assert len(calls) == 1  # fetch() was never called again -- it was a hit, not a re-fetch


def test_free_calls_are_never_budget_checked(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "monthly_budget_cents", 10)
    calls = []

    def fetch():
        calls.append(1)
        return {"free": True}

    # already over budget from the caller's perspective, but cost_cents=0 (the default)
    # means this call must never be blocked -- free providers are budget-exempt by design.
    resp = cache.cached("free-provider", "ep", {"q": 1}, fetch)
    assert resp == {"free": True}
    assert len(calls) == 1
