"""Runtime settings — lets you paste API keys in the dashboard instead of editing
.env + restarting. DB `setting` rows override the .env-loaded `settings` object
live; saving rebuilds the provider registry so new keys take effect immediately.
Precedence: DB override > .env > default."""
import importlib
import logging

from .config import settings
from .db import get_conn

logger = logging.getLogger(__name__)

# (name, label, kind) — kind: "secret" | "text" | "int" | "bool"
FIELDS = [
    ("anthropic_api_key", "Anthropic API key (AI search, chat, extraction)", "secret"),
    ("llm_model", "LLM model", "text"),
    ("voyage_api_key", "Voyage API key (semantic ranking — free tier covers this corpus)", "secret"),
    ("google_maps_key", "Google Maps key (Street View embed only)", "secret"),
    ("crawl_stealth", "Stealth tier for Cloudflare-walled sites (needs `scrapling install`)", "bool"),
    ("crawl_delay_seconds", "Per-domain crawl delay (seconds)", "text"),
    ("monthly_budget_cents", "Monthly paid-spend cap (cents)", "int"),
]
_KINDS = {name: kind for name, label, kind in FIELDS}
SECRETS = {name for name, _, kind in FIELDS if kind == "secret"}


def _apply(name: str, value: str) -> None:
    if not hasattr(settings, name):
        return
    if _KINDS.get(name) == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
    if _KINDS.get(name) == "bool":
        value = str(value).lower() in ("1", "true", "on", "yes")
    setattr(settings, name, value)  # live override on the shared settings object


def load_overrides() -> None:
    """Apply saved DB overrides onto `settings` at startup."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM setting").fetchall()
    for r in rows:
        _apply(r["key"], r["value"])


def save(updates: dict[str, str]) -> None:
    """Persist + apply updates, then rebuild providers so keys take effect now.

    `registry.reset()` only invalidates cached provider *instances* built with the old
    keys — it does not touch the DB write above, which is the part that actually matters
    (a pasted key must be saved and applied even before providers exist). `app/registry.py`
    doesn't land until Task 7, so until then this is a no-op — loudly logged, never a
    silent swallow, per this codebase's hard rule (a silent fallback hid a 400 for
    OpenProp's entire life)."""
    with get_conn() as conn:
        for name, value in updates.items():
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (name, str(value)),
            )
    for name, value in updates.items():
        _apply(name, str(value))
    try:
        # importlib.import_module (not `from . import registry`) so a genuinely missing
        # submodule raises ModuleNotFoundError with an exact `.name` we can check — a bare
        # `from . import registry` collapses a missing submodule into a plain ImportError
        # with no reliable way to tell it apart from a real bug inside registry.py.
        registry = importlib.import_module(".registry", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.registry":
            raise  # a *different* missing import inside registry.py — a real bug, don't hide it
        logger.warning(
            "settings_store.save(): skipped registry.reset() — app/registry.py does not exist "
            "yet (lands in Task 7). Settings were saved to the DB and applied to `settings` "
            "normally; only the provider-instance cache invalidation was skipped, and there "
            "are no provider instances to invalidate yet."
        )
    else:
        registry.reset()  # drop cached provider instances built with the old keys
