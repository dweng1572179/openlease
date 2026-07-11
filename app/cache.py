"""Cache-through + spend tracking — the "never pay twice" + budget-guardrail
layer (spec §6, §8). Every provider call goes through cached(). A cache hit is
free; a miss is the ONLY place money is spent, and paid misses are refused once
the monthly cap is hit."""
import hashlib
import json
import threading

from .config import settings
from .db import get_conn

_MISS = object()  # distinct from a stored JSON `null`, so a cached None is a real hit

# ponytail: one global lock serializes the check->fetch->record critical section.
# It closes the double-click race (two concurrent misses both billing / overrunning
# the cap). Fine for a single-user single-process app; go per-key if throughput
# ever matters. It does NOT coordinate across `uvicorn --workers>1` — the ON CONFLICT
# accumulate in cache_put is the backstop that keeps the meter honest if it does.
_LOCK = threading.Lock()


class BudgetExceeded(Exception):
    """Raised when a paid call would exceed MONTHLY_BUDGET_CENTS."""


def hash_request(provider: str, endpoint: str, req: dict) -> str:
    blob = json.dumps({"p": provider, "e": endpoint, "r": req}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def cache_get(key: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT response_json FROM provider_cache WHERE request_hash = ?", (key,)
        ).fetchone()
    return _MISS if row is None else json.loads(row["response_json"])


def cache_put(key: str, provider: str, endpoint: str, resp, cost_cents: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO provider_cache "
            "(provider, endpoint, request_hash, response_json, cost_cents) "
            "VALUES (?, ?, ?, ?, ?) "
            # backstop: if a duplicate paid fetch slipped through (multi-worker),
            # add its cost rather than dropping it, so spend stays accurate.
            "ON CONFLICT(request_hash) DO UPDATE SET cost_cents = cost_cents + excluded.cost_cents",
            (provider, endpoint, key, json.dumps(resp, default=str), cost_cents),
        )


def spend_this_month() -> int:
    """Sum of cost_cents for cache rows fetched in the current calendar month."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_cents), 0) AS c FROM provider_cache "
            "WHERE strftime('%Y-%m', fetched_at) = strftime('%Y-%m', 'now')"
        ).fetchone()
    return row["c"]


def budget_remaining_cents() -> int:
    return settings.monthly_budget_cents - spend_this_month()


def cached(provider: str, endpoint: str, req: dict, fetch, cost_cents: int = 0):
    """Return cached response if present; else fetch(), store, and return it.
    Paid fetches (cost_cents > 0) are blocked if they'd blow the monthly cap.
    cost_cents defaults to 0 — a FREE call; paid providers must pass their rate."""
    key = hash_request(provider, endpoint, req)
    hit = cache_get(key)
    if hit is not _MISS:
        return hit
    with _LOCK:
        hit = cache_get(key)  # re-check under the lock — a peer may have just filled it
        if hit is not _MISS:
            return hit
        if cost_cents > 0 and spend_this_month() + cost_cents > settings.monthly_budget_cents:
            raise BudgetExceeded(
                f"{provider}/{endpoint} would cost {cost_cents}c; "
                f"{budget_remaining_cents()}c left this month. Raise MONTHLY_BUDGET_CENTS to proceed."
            )
        # ponytail: if fetch() raises AFTER the provider billed (200 then bad JSON),
        # the cost isn't booked and a retry re-bills. Rare; accepted for occasional
        # single-user use — upgrade to a spent-but-unparseable ledger row if it bites.
        resp = fetch()  # the only line that spends money
        cache_put(key, provider, endpoint, resp, cost_cents)
        return resp
