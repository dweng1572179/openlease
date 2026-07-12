# OpenLease Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted, keyless-first, AI-native CRE leasing search over four metros (NYC, Miami, LA, Chicago), per `docs/design-spec.md`. **The app is this repo's root** — `app/`, `tests/`, `requirements.txt` sit at the top level.

**Architecture:** Modeled on OpenProp, file-for-file: FastAPI + Jinja + HTMX + Tailwind(CDN) + MapLibre, stdlib `sqlite3` (WAL, no ORM), providers behind Protocols selected by a lazy `registry`, every network call wrapped in `cache.cached()` with a monthly paid-spend cap. Four layers: **supply** (a generic fetch ladder over allowlisted broker sites + free government feeds), **enrichment** (per-metro parcel providers + metro-agnostic Walk/Transit scoring), **search** (LLM parse → hard SQL filter → FTS5 BM25 (+ optional cosine) fused with RRF → LLM reply), **AI workspace** (per-listing RAG chat, portfolios, export).

**Tech Stack:** Python 3.11+, FastAPI 0.115.6, uvicorn, pydantic 2.10.4 / pydantic-settings 2.7.0, Jinja2, httpx 0.28.1, `anthropic==0.116.0` (needs `messages.parse`), `scrapling[fetchers]==0.4.10`, `numpy` (cosine only), `openpyxl` (xlsx), SQLite FTS5 (stdlib), MapLibre GL (CDN).

**Before Task 1:** several files are lifted verbatim from OpenProp, which is its own repo
with the same root shape as this one (`app/`, `tests/`, `requirements.txt`). Clone it as a
**sibling directory** first, or the `cp ../openprop/...` commands in Task 1 and Task 14
will not resolve:

```bash
cd .. && git clone https://github.com/dweng1572179/openprop.git && cd openlease
```

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from the spec.

- **The app is the repo root.** Password env var `OPENLEASE_PASSWORD`. Default port **8788** (OpenProp holds 8787; both must be able to run at once). DB `openlease.db`.
- **Runs keyless.** Every layer must work with zero API keys. `ANTHROPIC_API_KEY` / `VOYAGE_API_KEY` / `GOOGLE_MAPS_KEY` are *unlocks*, never requirements. Keys are pasted in the Settings dashboard (DB `setting` table), which overrides `.env`.
- **Sentinels, not nulls,** in every `messages.parse()` schema: no `| None`, no defaults, every field required. `""` / `0` / `[]` mean "not mentioned." (>16 nullable params → 400; *any* optional param → the request **hangs**.)
- **Never authenticate** against a broker site. No login, cookie, account, registration- or NDA-gated content. Ever. No override flag exists.
- **Store facts, not expression.** Never persist broker marketing prose verbatim (we write `our_description` with the LLM). Never download or re-host listing photos — store the broker's own URL and hot-link it.
- **`robots.txt` is fetched, parsed, obeyed**, `Crawl-delay` honored. UA: `OpenLeaseBot/0.1 (+<repo>; <email>) self-hosted single-user`. Rate limit 1 req / 3–5s per domain, exp-backoff on 429/503, daily per-domain cap, conditional GETs, TTL ≥ 24h.
- **`None` ≠ 0 ≠ "lookup failed".** A metro that does not publish a field (LA owner names, Chicago suburb zoning, Miami municipal zoning) returns `null` **with a reason**, surfaced in the UI. Never a zero, never a silent empty.
- **An empty Overpass response is an ERROR, never a score of 0.** Allowlist only `overpass-api.de` and `overpass.kumi.systems`.
- **`bm25()` is NEGATIVE** → `ORDER BY bm25(listing_fts) ASC`. Fuse ranked lists with **RRF, k=60** — never a weighted sum.
- **No `sqlite-vec`** (needs `enable_load_extension`, absent on stock python.org macOS / pyenv / system python). No per-site CSS parsers. No request-time Overpass. No `overpass.osm.ch`.
- **Wire contract is SpaceFinder's, verbatim.** Columns are `snake_case` in SQLite, serialized to `camelCase` at the API boundary.
- Every LLM fallback is **loudly logged** — a silent fallback hid a 400 for OpenProp's entire life.

---

## File Structure

Everything below is relative to the repo root.

```
openlease/            <- the repo root itself
  app/
    __init__.py
    app.py            FastAPI, auth (one password + signed cookie), home        [T1]
    config.py         .env settings + inline-comment guard   [lift: openprop]   [T1]
    cache.py          cache-through + monthly budget cap     [lift: openprop]   [T1]
    settings_store.py dashboard keys override .env           [lift: openprop]   [T1]
    db.py             SQLite schema + persistence + hard filter                 [T1,T2,T3]
    models.py         Listing / ListingQuery / Parcel + camelCase API serializer[T2]
    seed.py           12 demo listings so search is testable before the crawler [T2]
    rank.py           FTS5 BM25 + RRF (+ cosine in T12)                         [T3,T12]
    ai.py             NL→ListingQuery, reply, description, highlights, RAG chat [T4,T13]
    routes_search.py  POST /api/search, GET /api/sessions                       [T5]
    routes_listings.py GET /api/listings/{id}, POST /api/listings/{id}/ask      [T6,T13]
    routes_settings.py Settings dashboard                    [lift: openprop]   [T1]
    routes_portfolios.py  saves / portfolios                                    [T13]
    routes_export.py  CSV/XLSX                               [lift: openprop]   [T13]
    routes_crawl.py   POST /api/crawl (admin)                                   [T10]
    routes_import.py  CSV import + government supply pulls                      [T11]
    registry.py       capability → provider; parcel_provider(metro)             [T7]
    score.py          walk + transit score (published methodology)              [T8]
    crawl.py          the fetch ladder (Scrapling Spider)                       [T10]
    extract.py        feed/HTML → Listing (structured fast paths, LLM last)     [T10]
    export.py         CSV/XLSX                               [lift: openprop]   [T13]
    providers/
      base.py         Protocols: ParcelProvider, PoiProvider, Geocoder, Embedder[T7]
      geosearch.py    NYC GeoSearch (keyless)                                   [T7]
      overpass.py     POIs, ingest-time only, cached forever                    [T7]
      osrm.py         airport drive times + power-law offline fallback          [T7]
      voyage.py       embeddings (optional key)                                 [T12]
      parcel_nyc.py parcel_miami.py parcel_la.py parcel_chicago.py              [T9]
      gov_nyc.py      Storefront Registry + ACRIS                               [T11]
    data/
      metros.yml      bbox, airports, zoning-source branches per metro          [T2]
      sources.yml     allowlisted broker sites + ladder rung + robots status    [T10]
      rail/{nyc,mia,la,chi}.json   bundled station points (~800, <100KB)        [T7]
      rail/refresh.py build-time regen from each agency's open data             [T7]
      elements.db     Scrapling adaptive-selector store (gitignored)            [T10]
    templates/        base, login, home, _results, _listing_card, listing,
                      settings, portfolios, _chat                               [T1,T6,T13]
  tests/
    test_smoke.py test_models.py test_rank.py test_ai.py test_search.py
    test_score.py test_parcel.py test_extract.py
    fixtures/         canned Overpass / wp-json / JSON-LD / parcel responses
  requirements.txt run.sh Dockerfile docker-compose.yml README.md .env.example
  .gitignore .dockerignore
  "Start OpenLease.command" "Start OpenLease.bat" "Stop OpenLease.command" "Stop OpenLease.bat"
  guide/            walkthrough (mirrors openprop/guide)                        [T14]
```

---

### Task 1: Skeleton — config, DB, auth, cache, settings dashboard, launchers

The whole boring half of the app, lifted from OpenProp. Ends with: `./run.sh` serves a
password-gated home page on :8788 while OpenProp still runs on :8787.

**Files:**
- Create: `app/__init__.py`, `app/config.py`, `app/db.py`, `app/cache.py`, `app/settings_store.py`, `app/app.py`, `app/routes_settings.py`
- Create: `app/templates/base.html`, `login.html`, `home.html`, `settings.html`
- Create: `requirements.txt`, `run.sh`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `.dockerignore`, `Start OpenLease.command`, `Start OpenLease.bat`, `Stop OpenLease.command`, `Stop OpenLease.bat`
- Test: `tests/conftest.py`, `tests/test_smoke.py`, `tests/test_cache.py`, `tests/test_config.py`,
  `tests/test_settings_store.py`

**Interfaces:**
- Produces: `config.settings` (a `Settings` instance); `db.get_conn()` (contextmanager yielding a `sqlite3.Connection` with `row_factory=sqlite3.Row`), `db.init_db()`, `db.SCHEMA` (a `str`, grown by later tasks); `cache.cached(provider, endpoint, req: dict, fetch: Callable, cost_cents: int = 0)`, `cache.BudgetExceeded`, `cache.spend_this_month() -> int`, `cache.budget_remaining_cents() -> int`; `app.app` (the `FastAPI`), `app.require_auth` (a dependency), `app.templates` (a `Jinja2Templates`); `settings_store.FIELDS`, `settings_store.save(updates: dict) -> None`, `settings_store.load_overrides() -> None`.

- [ ] **Step 1: Scaffold the directory and copy the four verbatim lifts**

`cache.py` and `settings_store.py` are copied byte-for-byte and then edited; `config.py`
keeps its `_drop_inline_comment` validator (load-bearing: without it a blank `SECRET_KEY=  # note`
in `.env` reads as truthy and no random key is ever generated).

```bash
mkdir -p app/providers app/templates app/data/rail tests/fixtures
touch app/__init__.py app/providers/__init__.py
cp ../openprop/app/cache.py app/cache.py
cp ../openprop/app/settings_store.py app/settings_store.py
cp ../openprop/app/routes_settings.py app/routes_settings.py
cp ../openprop/app/templates/settings.html app/templates/settings.html
cp ../openprop/app/templates/login.html app/templates/login.html
cp "../openprop/Start OpenProp.command" "Start OpenLease.command"
cp "../openprop/Start OpenProp.bat" "Start OpenLease.bat"
cp "../openprop/Stop OpenProp.command" "Stop OpenLease.command"
cp "../openprop/Stop OpenProp.bat" "Stop OpenLease.bat"
cp ../openprop/.dockerignore .dockerignore
chmod +x "Start OpenLease.command" "Stop OpenLease.command"
```

Then in the copied files, rename throughout (`openprop`→`openlease`, `OpenProp`→`OpenLease`,
`OPENPROP_`→`OPENLEASE_`, `8787`→`8788`):

```bash

sed -i '' 's/openprop/openlease/g; s/OpenProp/OpenLease/g; s/OPENPROP/OPENLEASE/g; s/8787/8788/g' \
  app/cache.py app/settings_store.py app/routes_settings.py \
  app/templates/settings.html app/templates/login.html \
  "Start OpenLease.command" "Start OpenLease.bat" "Stop OpenLease.command" "Stop OpenLease.bat"
```

`cache.py` needs no further edit — it is domain-agnostic. `settings_store.py` and
`routes_settings.py` get their field lists replaced in Step 4.

> **Correction (Task 1 review):** the `sed` above is not sufficient for `login.html` and
> `settings.html`. Two things it cannot catch:
> 1. **The brand name is split across markup** — `login.html`'s hero renders
>    `Open<span class="text-emerald-600">Prop</span>`. `sed 's/OpenProp/OpenLease/g'` needs
>    the literal substring `OpenProp` on one line; it can't match across a `<span>` tag
>    boundary, so this line silently survives the rename untouched. After the `sed` pass,
>    manually fix it to match `base.html`'s (correct) brand markup —
>    `Open<span class="text-sky-600">Lease</span>` — and rewrite the tagline for CRE leasing
>    (OpenProp's copied-verbatim tagline is "Self-hosted property intelligence.", which is
>    the wrong domain for this app).
> 2. **OpenProp's brand color (`emerald`) is not `OpenLease`'s.** `base.html` (Step 6, written
>    fresh for this app, not lifted) uses `sky` for the brand accent, links, and buttons.
>    `login.html` and `settings.html` are lifted from OpenProp and keep every `emerald-*`
>    Tailwind class as-is — `sed` has no rule for it because "emerald" isn't an
>    OpenProp/OpenLease name collision, it's a color choice. After the rename pass, grep both
>    files for `emerald` and replace every occurrence with the matching `sky` shade
>    (`emerald-600`→`sky-600`, `emerald-500`→`sky-500`, `emerald-700`→`sky-700`,
>    `emerald-200`/`emerald-50`/`emerald-800`→`sky-200`/`sky-50`/`sky-800`) so the two lifted
>    pages match the rest of the app.
>
> Also lifted in this step: `Stop OpenLease.command` / `Stop OpenLease.bat`. Their `pkill -f
> "uvicorn app.app:app"` / `taskkill /f /im uvicorn.exe` match **OpenProp's own process too**
> — it runs the identical module path — which defeats the entire point of giving OpenLease
> its own port (8788, so both apps run at once; see `.env.example`). `sed`'s
> `8787`→`8788` substitution does not touch these two files at all (their process-kill
> commands never mention a port number, only the module path / binary name). Rewrite both to
> kill by the port they actually own instead of by module path:
> - `Stop OpenLease.command`: `lsof -ti ":${OPENLEASE_PORT:-8788}"` piped to `kill`, not
>   `pkill -f "uvicorn app.app:app"`.
> - `Stop OpenLease.bat`: find the PID bound to `%OPENLEASE_PORT%` via `netstat -ano` +
>   `findstr`, then `taskkill /f /pid`, not `taskkill /f /im uvicorn.exe`.

- [ ] **Step 2: Write `config.py`**

```python
"""Settings from .env. One source of truth; providers read keys from here."""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openlease_password: str = "changeme"
    secret_key: str = ""
    session_https_only: bool = False  # set true when serving over HTTPS (adds Secure to the cookie)

    # keys — every one optional; blank = that unlock is off, the app still runs
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"  # or claude-haiku-4-5 for cheaper parsing
    voyage_api_key: str = ""            # semantic ranking; blank = pure BM25
    google_maps_key: str = ""           # Street View embed only

    # crawler
    crawl_user_agent: str = (
        "OpenLeaseBot/0.1 (+https://github.com/fintok/openlease; openlease@example.com) "
        "self-hosted single-user"
    )
    crawl_delay_seconds: float = 4.0    # 1 req / 3-5s per domain (spec floor)
    crawl_daily_cap_per_domain: int = 500
    crawl_stealth: bool = True          # ON by default (spec); needs `scrapling install`

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    osrm_url: str = "https://router.project-osrm.org"

    monthly_budget_cents: int = 1000
    db_path: str = "openlease.db"

    @field_validator("*", mode="before")
    @classmethod
    def _drop_inline_comment(cls, v, info):
        """`KEY=   # note` in .env yields the comment as the value: python-dotenv only
        strips an inline comment when the value is non-empty. Left alone, a blank
        SECRET_KEY reads as truthy and app.py never generates a random one."""
        if isinstance(v, str) and v.lstrip().startswith("#"):
            return cls.model_fields[info.field_name].default
        return v


settings = Settings()


if __name__ == "__main__":  # python -m app.config
    import os
    os.environ |= {"SECRET_KEY": "  # leave blank -> generated", "MONTHLY_BUDGET_CENTS": "250"}
    s = Settings(_env_file=None)
    assert s.secret_key == "", f"comment leaked into secret_key: {s.secret_key!r}"
    assert s.monthly_budget_cents == 250, s.monthly_budget_cents
    assert Settings(_env_file=None, voyage_api_key="abc123").voyage_api_key == "abc123"
    print("config ok — inline comments dropped, real values preserved")
```

- [ ] **Step 3: Write `db.py` (skeleton schema — Tasks 2/3/13 append to `SCHEMA`)**

```python
"""SQLite: listings + parcels + sessions + cache + workspace. Raw stdlib sqlite3 —
no ORM, no migrations. Schema is spec §5. Single file, WAL mode."""
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_cache (
    id            INTEGER PRIMARY KEY,
    provider      TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    request_hash  TEXT NOT NULL UNIQUE,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    cost_cents    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_month ON provider_cache(fetched_at);

-- Runtime config (API keys) editable from the dashboard; overrides .env.
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
```

- [ ] **Step 4: Replace the settings field list**

In `app/settings_store.py`, replace the whole `FIELDS` list with OpenLease's
(everything else in the file — `_apply`, `load_overrides`, `save`, `SECRETS` — stays as
lifted, **except** `save()`'s `registry.reset()` call — see the correction below):

> **Correction (Task 1 review):** "stays as lifted" is wrong for one line. OpenProp's
> `save()` ends with an unconditional `registry.reset()` — correct there, because
> `registry.py` already exists in OpenProp. In OpenLease, `registry.py` doesn't land until
> Task 7, so left unconditional, `save()` raises on the very first `POST /settings`: a user
> pastes an API key and clicking Save 500s, and the key is never applied. The DB write above
> `registry.reset()` is the part that actually matters here (the key must save and apply even
> before providers exist) — `registry.reset()` is only provider-instance cache invalidation,
> and there are no provider instances yet. Make the reset conditional on `registry.py`
> existing, and **log the skip at WARNING**, naming exactly what didn't run and why (this
> codebase's hard rule: every fallback is loud, never a silent swallow — a silent fallback
> hid a 400 for OpenProp's entire life). Use `importlib.import_module(".registry",
> __package__)` rather than `from . import registry` — the latter collapses a genuinely
> missing submodule into a plain `ImportError` with no reliable `.name` to check, so a real
> bug inside a future `registry.py` would get silently swallowed right along with the
> expected Task-1 gap:
> ```python
> import importlib
> ...
> def save(updates: dict[str, str]) -> None:
>     ...  # DB write + _apply(), unchanged
>     try:
>         registry = importlib.import_module(".registry", __package__)
>     except ModuleNotFoundError as exc:
>         if exc.name != f"{__package__}.registry":
>             raise
>         logger.warning(
>             "settings_store.save(): skipped registry.reset() — app/registry.py does not "
>             "exist yet (lands in Task 7); settings were saved and applied normally."
>         )
>     else:
>         registry.reset()
> ```

```python
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
```

`_apply` currently only coerces `"int"`. Add the `bool` branch right beside it:

```python
    if _KINDS.get(name) == "bool":
        value = str(value).lower() in ("1", "true", "on", "yes")
```

In `routes_settings.py`, replace `_status()` with OpenLease's capability list (`registry`
lands in Task 7, so import lazily inside the function and treat a missing attribute as off):

```python
def _status() -> list[dict]:
    """What's active right now, so the user can see keys took effect."""
    from . import ai
    return [
        {"label": "Listings search (BM25)", "on": True, "note": "free, no key"},
        {"label": "Parcel data — NYC / Miami / LA / Chicago", "on": True, "note": "free, no key"},
        {"label": "Walk + Transit score (Overpass)", "on": True, "note": "free, no key"},
        {"label": "AI (parse / reply / extract / chat)", "on": ai.available(),
         "note": "needs ANTHROPIC_API_KEY (rules parser otherwise — understands far less)"},
        {"label": "Semantic ranking (Voyage)", "on": bool(settings.voyage_api_key),
         "note": "optional; BM25-only without it"},
        {"label": "Street View", "on": bool(settings.google_maps_key), "note": "optional"},
    ]
```

`ai.py` does not exist until Task 4 — for this task only, stub it so the import resolves:

```python
# app/ai.py  (Task 4 replaces this file entirely)
from .config import settings


def available() -> bool:
    return bool(settings.anthropic_api_key)
```

Also drop the `bool` kind into `routes_settings.settings_save`, which currently only special-cases
`select:` — an unchecked HTML checkbox posts nothing, so a blank must mean False, not "keep":

```python
        if kind.startswith("select:") or kind == "bool":
            updates[name] = v
        elif v:
            updates[name] = v
```

and in `settings.html`, add the checkbox branch next to the existing select/secret/text branches:

```html
        {% elif f.kind == "bool" %}
          <input type="checkbox" name="{{ f.name }}" value="true" {% if f.value %}checked{% endif %}
                 class="h-4 w-4 rounded border-slate-300">
```

`routes_settings.py`'s two `TemplateResponse` calls (`settings_page`, `settings_save`) take the
same new-signature fix as `app.py`'s (see the Step 5 correction below):
`templates.TemplateResponse(request, "settings.html", _ctx(request))`, with `request` passed
positionally rather than embedded in `_ctx()`'s context dict.

- [ ] **Step 5: Write `app.py`**

Identical in shape to `../openprop/app/app.py` — same `_Redirect` exception handler, same
`SessionMiddleware`, same startup hook — with OpenLease's names and route imports:

```python
"""FastAPI app: single service, single user. Routes stay thin — data access in db.py,
provider calls behind registry.py. Auth is one password + a signed session cookie."""
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .cache import budget_remaining_cents, spend_this_month
from .config import settings
from .db import init_db


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    from . import settings_store
    settings_store.load_overrides()
    yield


app = FastAPI(title="OpenLease", lifespan=_lifespan)

_secret = settings.secret_key or secrets.token_hex(32)
if not settings.secret_key:
    print("[openlease] no SECRET_KEY set — using an ephemeral one "
          "(sessions reset on restart). Set SECRET_KEY in .env to persist.")
if settings.openlease_password == "changeme":
    print("[openlease] WARNING: OPENLEASE_PASSWORD is still the default 'changeme' — "
          "set a real password in .env before exposing this beyond localhost.")
app.add_middleware(
    SessionMiddleware, secret_key=_secret, same_site="lax",
    https_only=settings.session_https_only,
)

templates = Jinja2Templates(directory="app/templates")


# --- auth --------------------------------------------------------------------

class _Redirect(Exception):
    def __init__(self, to: str):
        self.to = to


def require_auth(request: Request):
    """Dependency: bounce unauthenticated requests to /login."""
    if not request.session.get("auth"):
        raise _Redirect("/login")
    return True


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.to, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, settings.openlease_password):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Wrong password."}, status_code=401
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- home --------------------------------------------------------------------

def spend_ctx() -> dict:
    return {
        "spend_cents": spend_this_month(),
        "budget_cents": settings.monthly_budget_cents,
        "remaining_cents": budget_remaining_cents(),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(
        request, "home.html", {"metro": "nyc", **spend_ctx()}
    )


# Feature routes attach to `app` here as each task lands:
from . import routes_settings   # noqa: E402,F401  (T1)
```

> **Correction (Task 1 review):** OpenProp's `app.py` used `@app.on_event("startup")` and the
> old-style `TemplateResponse(name, {"request": request, ...})` call signature. Both are
> deprecated in the FastAPI/Starlette versions this plan pins (`fastapi==0.115.6` triggers the
> `on_event` `DeprecationWarning`; the installed `starlette` triggers the `TemplateResponse`
> one) — copying them verbatim makes every test run emit 5 standing warnings, which would mask
> a real new warning in any of the next 13 tasks. Fixed in the block above: a `@asynccontextmanager`
> `_lifespan` passed to `FastAPI(..., lifespan=_lifespan)` replaces `@app.on_event("startup")`,
> and every `TemplateResponse` call takes `request` as its first positional argument with
> `request` dropped from the context dict (`routes_settings.py`'s two `TemplateResponse` calls
> in Step 4 get the identical fix). Verified: `pytest -v` after this fix reports `0 warnings`.

- [ ] **Step 6: Write `templates/base.html` and a placeholder `home.html`**

`base.html` — same as OpenProp's plus MapLibre's CSS/JS and a wider container (the app is
map-first, not a 5xl reading column):

```html
<!doctype html>
<html lang="en" class="h-full">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}OpenLease{% endblock %}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
  <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
</head>
<body class="h-full bg-slate-50 text-slate-800">
  <header class="border-b bg-white">
    <div class="mx-auto max-w-7xl px-4 py-3 flex items-center justify-between">
      <a href="/" class="font-semibold text-lg tracking-tight">Open<span class="text-sky-600">Lease</span></a>
      <div class="flex items-center gap-4 text-sm">
        {% if budget_cents is defined %}
        <span class="text-slate-500" title="Paid provider spend this calendar month">
          spend: <b>${{ "%.2f"|format(spend_cents/100) }}</b> / ${{ "%.2f"|format(budget_cents/100) }}
        </span>
        {% endif %}
        <a href="/settings" class="text-slate-400 hover:text-slate-700">settings</a>
        <a href="/logout" class="text-slate-400 hover:text-slate-700">logout</a>
      </div>
    </div>
  </header>
  <main class="mx-auto max-w-7xl px-4 py-6">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

`home.html` — a placeholder Task 6 replaces with the real search UI. It must contain the
string the smoke test asserts on:

```html
{% extends "base.html" %}
{% block content %}
<h1 class="text-xl font-semibold mb-2">Describe the space you need</h1>
<p class="text-sm text-slate-500">Search lands in Task 5.</p>
{% endblock %}
```

- [ ] **Step 7: Write the infra files**

`requirements.txt`:

```
# OpenLease — self-hosted CRE leasing search. Lean by design.
# SQLite (+FTS5) is stdlib; HTMX/Tailwind/MapLibre are CDN (no JS build).
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
pydantic-settings==2.7.0
jinja2==3.1.5
itsdangerous==2.2.0          # signed session cookies (Starlette SessionMiddleware)
httpx==0.28.1
python-multipart==0.0.20     # form posts from HTMX
pyyaml==6.0.2                # metros.yml / sources.yml
openpyxl==3.1.5              # xlsx export
numpy==2.2.1                 # brute-force cosine over the embedding matrix (T12)
anthropic==0.116.0           # needs messages.parse (structured outputs); AI degrades without a key
scrapling[fetchers]==0.4.10   # the fetch ladder; `scrapling install` adds the stealth browser
```

> **Correction (Task 1, verified live):** the brief originally pinned `scrapling[fetchers,ai]`.
> The `ai` extra pulls in `mcp>=1.27.0`, which requires `pydantic>=2.11.0` — a hard conflict
> with this plan's pinned `pydantic==2.10.4` (`pip install` fails with `ResolutionImpossible`).
> Nothing in this codebase uses scrapling's AI/MCP shell (Task 10 only imports
> `scrapling.fetchers.{FetcherSession,StealthySession}` and `scrapling.parser.Selector`, both
> covered by the `fetchers` extra), so the fix is to drop the unused `ai` extra rather than
> bump pydantic. `requirements.txt` and `docs/implementation-plan.md` both corrected in the
> Task 1 commit.

`run.sh` (`chmod +x`):

```bash
#!/bin/bash
# Start OpenLease locally. Default port 8788 (OpenProp holds 8787); override with
# OPENLEASE_PORT=9000 ./run.sh
set -e
cd "$(dirname "$0")"
PORT="${OPENLEASE_PORT:-8788}"
[ -x .venv/bin/uvicorn ] && UV=.venv/bin/uvicorn || UV=uvicorn
echo "OpenLease -> http://localhost:$PORT   (Ctrl-C to stop)"
exec "$UV" app.app:app --host 127.0.0.1 --port "$PORT"
```

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/app/data/openlease.db
EXPOSE 8000
# data/ is a mounted volume (see docker-compose) so listings + cache survive restarts
CMD ["sh", "-c", "mkdir -p /app/data && uvicorn app.app:app --host 0.0.0.0 --port 8000"]
```

`docker-compose.yml`:

```yaml
# docker compose up  ->  http://localhost:8788
services:
  openlease:
    build: .
    ports:
      - "${OPENLEASE_PORT:-8788}:8000"   # host -> container (container stays on 8000)
    env_file: .env
    volumes:
      - openlease-data:/app/data
    restart: unless-stopped

volumes:
  openlease-data:
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.ruff_cache/
.env
openlease.db
openlease.db-*
*.db
app/data/elements.db
.DS_Store

# never commit a real env; .env alone does not match .env.bak/.env.local
.env.*
!.env.example
```

`.env.example`:

```
# OpenLease config — copy to .env and fill what you have. Everything is optional;
# the app runs on free government data with no keys at all.
#
# NOTE: a comment must go ABOVE a blank key, never beside it. `KEY=  # note` makes
# the comment the value (python-dotenv only strips an inline comment after a value).

# --- single-user auth ---
OPENLEASE_PASSWORD=changeme       # CHANGE THIS before exposing beyond localhost
# leave blank -> a random key is generated & printed on first run
SECRET_KEY=
SESSION_HTTPS_ONLY=false

# --- keys (bring your own; blank = that unlock is off, the app still runs) ---
# NOTE: the /settings page writes these into the DB `setting` table, which OVERRIDES
# this file at runtime. If an edit here seems to do nothing, change it on /settings.
ANTHROPIC_API_KEY=                # NL search, conversational reply, LLM extraction, chat
LLM_MODEL=claude-opus-4-8         # or claude-haiku-4-5 for cheaper parsing
VOYAGE_API_KEY=                   # semantic ranking; free tier covers this corpus ~400x
GOOGLE_MAPS_KEY=                  # Street View embed only

# --- crawler (see app/data/sources.yml for the allowlist) ---
# Stealth needs a one-time `scrapling install` (~400-600MB Chromium). Without it the
# crawler still works — it just skips the Cloudflare-walled sites.
CRAWL_STEALTH=true
CRAWL_DELAY_SECONDS=4
CRAWL_DAILY_CAP_PER_DOMAIN=500

# --- cost guardrails ---
MONTHLY_BUDGET_CENTS=1000         # hard cap on paid spend per calendar month

# DB_PATH is deliberately NOT set here. config.py's own default (./openlease.db) is right
# for `./run.sh` (local). In Docker, docker-compose.yml loads this whole file via
# `env_file: .env`, and env_file entries OVERRIDE the image's `ENV DB_PATH=/app/data/openlease.db`
# (Dockerfile) -- so an active DB_PATH= line here would silently redirect the DB outside the
# `openlease-data` volume and it would be destroyed on every `docker compose down`. If you need
# a custom path for local (non-Docker) use, uncomment and set an absolute path:
# DB_PATH=openlease.db

# --- server ---
OPENLEASE_PORT=8788               # OpenProp holds 8787; both can run at once
```

> **Correction (Task 1 review):** the brief originally shipped an active `DB_PATH=openlease.db`
> line here. `docker-compose.yml`'s `env_file: .env` loads this file into the container and
> **overrides** the Dockerfile's `ENV DB_PATH=/app/data/openlease.db` whenever the loaded file
> actually sets `DB_PATH` — Compose's documented precedence is `env_file` above the image's
> `ENV`. With the line active, `cp .env.example .env && docker compose up` (the documented
> path) writes SQLite to `/app/openlease.db`, **outside** the `openlease-data` volume mounted
> at `/app/data` — the DB is destroyed on every `docker compose down && up`, contradicting the
> Dockerfile's own "listings + cache survive restarts" comment. Commenting the line out (with
> the explanation above) is the fix: `config.py`'s default wins locally, the Dockerfile's `ENV`
> wins in Docker, and both paths persist correctly. Verified: `.venv/bin/python -c
> "from app.config import Settings; print(Settings(_env_file=None).db_path)"` with a `.env`
> built from the corrected example prints `openlease.db`, matching the local default; Docker
> itself was not available in the Task-1-fix environment to run `docker compose up` end to end
> (see the Task 1 fix report), so the container side is verified by inspection of Compose's
> documented `env_file`-vs-`ENV` precedence, not by a live run.

- [ ] **Step 8: Write the failing smoke test — and the load-bearing unit tests**

`tests/conftest.py` — shared bootstrap. `app.config.settings` is a process-wide singleton
created on the *first* import of `app.config`, from whichever test module pytest collects
first (alphabetical by default). Putting the env bootstrap in `conftest.py` — which pytest
always imports before any test file in its directory — is what guarantees every test file
sees the same base settings regardless of collection order; a per-file bootstrap (as an
earlier draft of this plan had, duplicated at the top of `test_smoke.py`) breaks the moment
a second test file is added and happens to collect first:

```python
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
```

`tests/test_smoke.py`:

```python
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
```

`tests/test_cache.py`, `tests/test_config.py`, `tests/test_settings_store.py` — the smoke test
alone only exercises auth + page rendering. Three behaviors this file's own Interfaces section
calls load-bearing had no automated test until a Task 1 review caught the gap: `cache.cached()`'s
monthly budget cap (a paid miss over budget raises `BudgetExceeded`; a cache **hit** is never
refused, even over budget — "you never pay twice"); `config.py`'s `_drop_inline_comment` guard
(a `.env` value of `SECRET_KEY=  # note` must not read as truthy); and
`settings_store.load_overrides()` (a DB `setting` row must override whatever `.env` set). See
the three test files for the full cases — each isolates its own scratch DB via the `monkeypatch`
fixture (`monkeypatch.setattr(settings, "db_path", ...)`) so tests can't leak state into each
other regardless of run order.

- [ ] **Step 9: Run it — expect failure**

```bash
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest tests/ -v
```

Expected on first run: FAIL — `ModuleNotFoundError` or a missing template, until every file
above exists. Iterate until it passes.

- [ ] **Step 10: Run the config self-check and the full test suite**

```bash
.venv/bin/python -m app.config && .venv/bin/python -m pytest tests/ -v
```

Expected: `config ok — inline comments dropped, real values preserved` then `9 passed` with
**no warnings** (`@app.on_event`/old-style `TemplateResponse` calls, both deprecated, would
otherwise print 5 `DeprecationWarning`s here — fixed in Step 5's `app.py` and in
`routes_settings.py`; see the Step 5 correction).

- [ ] **Step 11: Verify both apps run side by side**

```bash
./run.sh &
sleep 3 && curl -s -o /dev/null -w "openlease:%{http_code}\n" http://localhost:8788/login
kill %1
```

Expected: `openlease:200`. (OpenProp on 8787 is untouched.)

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "feat(openlease): skeleton — auth, SQLite, cache+budget, settings dashboard, launchers on :8788"
```

---

### Task 2: Domain models, metros.yml, listing persistence, camelCase boundary

The data contract. `snake_case` in SQLite, **SpaceFinder's `camelCase` on the wire** — so a
client written against SpaceFinder's `/api/search` sees identical objects.

**Files:**
- Create: `app/models.py`, `app/data/metros.yml`, `app/seed.py`
- Modify: `app/db.py` (append to `SCHEMA`; add listing persistence)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `db.get_conn`, `db.SCHEMA` (Task 1).
- Produces:
  - `models.Listing` (pydantic, snake_case fields), `models.ListingQuery`, `models.Parcel`
  - `models.to_api(row: dict) -> dict` — snake→camel, `our_description`→`description`, `photo_urls_json`→`photos`
  - `models.METROS: dict[str, dict]` loaded from `data/metros.yml`; keys `nyc|mia|la|chi`
  - `db.save_listing(rec: dict) -> int` (upsert on `source_url`), `db.get_listing(listing_id: int) -> dict | None`
  - `seed.seed() -> int` — inserts 12 demo listings, returns the count

- [ ] **Step 1: Write `data/metros.yml`**

Values are the spec's verified appendix (§7). `zoning_source` records *why* a field can be
`null` — the UI shows that reason instead of an empty box.

```yaml
# Per-metro constants. Verified 2026-07-11 (spec §7).
nyc:
  name: New York City
  bbox: [40.4774, -74.2591, 40.9176, -73.7002]   # min_lat, min_lng, max_lat, max_lng
  center: [40.7580, -73.9855]
  airports:
    JFK: [40.6413, -73.7781]
    LGA: [40.7769, -73.8740]
    EWR: [40.6895, -74.1745]
  parcel_key: bbl
  owner_available: true
  zoning_available: true
  zoning_source: NYC PLUTO (citywide)
  boroughs: [Manhattan, Brooklyn, Queens, Bronx, Staten Island]
mia:
  name: Miami
  bbox: [25.55, -80.50, 25.98, -80.10]
  center: [25.7743, -80.1937]
  airports:
    MIA: [25.7959, -80.2870]
    FLL: [26.0742, -80.1506]
  parcel_key: folio
  owner_available: true
  zoning_available: partial
  # County zoning returns 0 features inside incorporated cities (Brickell, Wynwood,
  # Downtown). We branch to the municipal layer; outside a known branch, zoning is null.
  zoning_source: Miami-Dade county layer (unincorporated) + M21_Zoning (City of Miami)
  boroughs: [Miami, Miami Beach, Coral Gables, Doral, Hialeah]
la:
  name: Los Angeles
  bbox: [33.70, -118.70, 34.35, -117.85]
  center: [34.0522, -118.2437]
  airports:
    LAX: [33.9416, -118.4085]
    BUR: [34.2007, -118.3587]
    LGB: [33.8177, -118.1516]
    SNA: [33.6757, -117.8683]
    ONT: [34.0560, -117.6012]
  parcel_key: ain
  owner_available: false
  # California statute: owner-of-record is not free/public via the county's open GIS.
  owner_source: null (California statute — not published free; not a lookup failure)
  zoning_available: true
  zoning_source: LA County ArcGIS zoning layer (no FAR)
  boroughs: [Downtown, Hollywood, Westside, San Fernando Valley, South Bay]
chi:
  name: Chicago
  bbox: [41.469, -88.264, 42.154, -87.524]
  center: [41.8781, -87.6298]
  airports:
    ORD: [41.9742, -87.9073]
    MDW: [41.7868, -87.7522]
  parcel_key: pin
  owner_available: true
  zoning_available: partial
  # Zoning/floors/FAR are a City of Chicago dataset: null for ~half the county (suburbs).
  zoning_source: City of Chicago zoning (city limits only — null for suburban Cook)
  boroughs: [Loop, West Loop, River North, Lincoln Park, Hyde Park]
```

- [ ] **Step 2: Write `models.py`**

```python
"""Normalized domain models + the camelCase API boundary.

SQLite stores snake_case. SpaceFinder's wire contract is camelCase. `to_api()` is the
only place the two meet, so nothing else in the app has to think about it.

Two deliberate divergences from SpaceFinder's schema (spec §5, CoStar v. CREXi):
  1. no `description` column — the broker's marketing prose is NEVER persisted. We store
     `our_description` (LLM-written) and serialize it AS `description`.
  2. `photo_urls` are the broker's own URLs, referenced and hot-linked — never downloaded
     or re-hosted. They serialize as `photos[]`.
The client sees SpaceFinder's object either way.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

METROS: dict[str, dict] = yaml.safe_load(
    (Path(__file__).parent / "data" / "metros.yml").read_text()
)
METRO_KEYS = tuple(METROS)  # ("nyc", "mia", "la", "chi")


class Listing(BaseModel):
    """The ~35-field observed SpaceFinder schema, minus the copyright traps."""
    id: int | None = None
    source: str | None = None            # sources.yml key, "csv", or "nyc_storefront"
    source_url: str                      # UNIQUE — the dedup key
    status: str = "available"
    metro: str                           # nyc | mia | la | chi

    property_type: str | None = None     # retail | office | industrial | flex | land
    subtype: str | None = None
    transaction_type: str = "lease"      # lease | sale

    address: str
    neighborhood: str | None = None
    borough: str | None = None
    lat: float | None = None
    lng: float | None = None

    size_sf: int | None = None
    divisible_min_sf: int | None = None
    divisible_max_sf: int | None = None
    total_building_sf: int | None = None
    floor: str | None = None
    ceiling_height_ft: float | None = None

    asking_rent: float | None = None
    rent_unit: str | None = None         # "sf_yr" | "sf_mo" | "mo"
    lease_type: str | None = None        # NNN | modified gross | gross
    sale_price: int | None = None
    availability_date: str | None = None
    lease_term_months: int | None = None
    condition: str | None = None

    broker_name: str | None = None
    broker_firm: str | None = None
    broker_phone: str | None = None
    broker_email: str | None = None

    features_json: str | None = None
    brochure_url: str | None = None
    our_description: str | None = None   # LLM-written; NEVER the broker's prose
    highlights_json: str | None = None   # LLM
    photo_urls_json: str | None = None   # external references only — never downloaded

    parcel_id: str | None = None
    walk_score: int | None = None
    transit_score: int | None = None
    score_breakdown_json: str | None = None
    semantic_score: float | None = None
    score: float | None = None
    rationale: str | None = None

    first_seen: str | None = None
    last_seen: str | None = None


class ListingQuery(BaseModel):
    """`query.mustHaves` — SpaceFinder's field names, verbatim. Serialize with
    `model_dump(by_alias=True)` to hit the wire contract."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    property_types: list[str] = Field(default_factory=list)
    transaction_type: str = "lease"
    boroughs: list[str] = Field(default_factory=list)
    neighborhood: str = ""
    min_size_sf: int = 0
    max_size_sf: int = 0
    max_rent_per_sf_yr: float = 0
    min_lat: float = 0
    max_lat: float = 0
    min_lng: float = 0
    max_lng: float = 0
    exclude_addr_states: list[str] = Field(default_factory=list)
    exclude_zip3: list[str] = Field(default_factory=list)
    exclude_cities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)   # feeds BM25; not a hard filter


class Parcel(BaseModel):
    """`None` means THIS METRO DOES NOT PUBLISH THIS FIELD — never 'lookup failed',
    never 0. `missing_reason` carries the why, straight to the UI."""
    parcel_id: str              # metro-prefixed, e.g. "nyc:1000160100"
    metro: str
    owner_name: str | None = None
    zoning: str | None = None
    far_built: float | None = None
    far_allowed: float | None = None
    year_built: int | None = None
    lot_sqft: int | None = None
    bldg_sqft: int | None = None
    floors: int | None = None
    units: int | None = None
    use_code: str | None = None
    missing_reason: dict[str, str] = Field(default_factory=dict)  # field -> why it's null
    raw_json: str | None = None


# --- the camelCase boundary ---------------------------------------------------

_JSON_COLS = {
    "features_json": "features",
    "highlights_json": "highlights",
    "photo_urls_json": "photos",
    "score_breakdown_json": "scoreBreakdown",
}
# An empty JSON column decodes to its container's empty value, not to []. score_breakdown
# is the per-category walk-score dict (Task 8); serving it as [] would hand the UI a list
# where it iterates .items().
_JSON_EMPTY: dict[str, list | dict] = {"score_breakdown_json": {}}
_RENAME = {"our_description": "description"}   # we serve OUR prose under their key


def to_api(row: dict) -> dict:
    """DB row (snake_case, JSON-as-text) -> SpaceFinder's listing object (camelCase)."""
    out: dict = {}
    for k, v in dict(row).items():
        if k in _JSON_COLS:
            out[_JSON_COLS[k]] = json.loads(v) if v else _JSON_EMPTY.get(k, [])
        elif k in _RENAME:
            out[_RENAME[k]] = v
        else:
            out[to_camel(k)] = v
    return out
```

- [ ] **Step 3: Append the listing schema to `db.py`'s `SCHEMA` and add persistence**

Append to the `SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS listing (
    id                  INTEGER PRIMARY KEY,
    source              TEXT,
    source_url          TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'available',
    metro               TEXT NOT NULL,
    property_type       TEXT, subtype TEXT,
    transaction_type    TEXT NOT NULL DEFAULT 'lease',
    address             TEXT NOT NULL,
    neighborhood        TEXT, borough TEXT, lat REAL, lng REAL,
    size_sf             INTEGER, divisible_min_sf INTEGER, divisible_max_sf INTEGER,
    total_building_sf   INTEGER, floor TEXT, ceiling_height_ft REAL,
    asking_rent         REAL, rent_unit TEXT, lease_type TEXT, sale_price INTEGER,
    availability_date   TEXT, lease_term_months INTEGER, condition TEXT,
    broker_name         TEXT, broker_firm TEXT, broker_phone TEXT, broker_email TEXT,
    features_json       TEXT, brochure_url TEXT,
    our_description     TEXT,   -- LLM-written. The broker's prose is NEVER stored.
    highlights_json     TEXT,
    photo_urls_json     TEXT,   -- external URLs, hot-linked. NEVER downloaded/re-hosted.
    parcel_id           TEXT,
    walk_score          INTEGER, transit_score INTEGER, score_breakdown_json TEXT,
    semantic_score      REAL, score REAL, rationale TEXT,
    first_seen          TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_listing_metro ON listing(metro, status);
CREATE INDEX IF NOT EXISTS idx_listing_geo   ON listing(lat, lng);

CREATE TABLE IF NOT EXISTS parcel (
    parcel_id   TEXT PRIMARY KEY,   -- metro-prefixed: "nyc:1000160100"
    metro       TEXT NOT NULL,
    owner_name  TEXT, zoning TEXT, far_built REAL, far_allowed REAL,
    year_built  INTEGER, lot_sqft INTEGER, bldg_sqft INTEGER,
    floors      INTEGER, units INTEGER, use_code TEXT,
    missing_reason_json TEXT,
    raw_json    TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Then append the persistence functions to `db.py`:

```python
# --- listing persistence (upsert by source_url) -------------------------------

_LISTING_COLS = [
    "source", "source_url", "status", "metro", "property_type", "subtype",
    "transaction_type", "address", "neighborhood", "borough", "lat", "lng",
    "size_sf", "divisible_min_sf", "divisible_max_sf", "total_building_sf", "floor",
    "ceiling_height_ft", "asking_rent", "rent_unit", "lease_type", "sale_price",
    "availability_date", "lease_term_months", "condition", "broker_name", "broker_firm",
    "broker_phone", "broker_email", "features_json", "brochure_url", "our_description",
    "highlights_json", "photo_urls_json", "parcel_id", "walk_score", "transit_score",
    "score_breakdown_json", "semantic_score", "score", "rationale",
]
_JSON_FIELDS = ("features_json", "highlights_json", "photo_urls_json", "score_breakdown_json")
# Columns with `NOT NULL DEFAULT` in SCHEMA: an explicit NULL in an INSERT's VALUES list
# bypasses a column's SQL DEFAULT (defaults only apply when the column is *omitted*), so
# a bare `:status` placeholder would violate the NOT NULL constraint whenever `rec` doesn't
# set it. COALESCE the placeholder itself down to the same literal the schema declares.
_SQL_DEFAULTS = {"status": "'available'", "transaction_type": "'lease'"}


def save_listing(rec: dict) -> int:
    """Upsert a normalized listing dict; return its row id. A re-crawl of the same
    source_url refreshes the row and bumps last_seen (that's the recrawl signal)."""
    row = {k: rec.get(k) for k in _LISTING_COLS}
    for k in _JSON_FIELDS:
        if isinstance(row.get(k), (list, dict)):
            row[k] = json.dumps(row[k])
    cols = ", ".join(_LISTING_COLS)
    placeholders = ", ".join(
        f"COALESCE(:{c}, {_SQL_DEFAULTS[c]})" if c in _SQL_DEFAULTS else f":{c}"
        for c in _LISTING_COLS
    )
    # Never overwrite a good value with a NULL from a thinner re-crawl. Reference the raw
    # bound parameter (`:col`), not `excluded.col`: `excluded.col` is the *post-default*
    # value from the VALUES clause above, which for status/transaction_type is never NULL
    # — using it here would silently reset an existing 'leased' status back to 'available'
    # on every re-crawl that doesn't repeat it. `:col` is the caller's raw input, so
    # "didn't mention it" still means "leave the stored value alone" for every column.
    updates = ", ".join(
        f"{c}=COALESCE(:{c}, {c})" for c in _LISTING_COLS if c != "source_url"
    )
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO listing ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(source_url) DO UPDATE SET {updates}, last_seen=datetime('now') "
            f"RETURNING id",
            row,
        )
        return cur.fetchone()["id"]


def get_listing(listing_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM listing WHERE id = ?", (listing_id,)).fetchone()
    return dict(row) if row else None
```

Add `import json` to `db.py`'s imports.

**Correction (2026-07-11, Task 2 implementation):** the code above as originally drafted
raised `sqlite3.IntegrityError: NOT NULL constraint failed: listing.status` on the very
first `seed()` call — the DEMO listings never set `status`, and an explicit `NULL` in an
`INSERT`'s `VALUES` list bypasses a column's SQL `DEFAULT` (the default only applies when
the column is omitted from the statement entirely). A plain `COALESCE(excluded.col, col)`
fix would have "worked" but reintroduced the exact bug the comment above it warns against:
`excluded.status` is the *post-default* value, never NULL, so it would silently reset a
listing's status back to `'available'` on every re-crawl that doesn't repeat it. The fix
applied instead: default `status`/`transaction_type` at the placeholder level
(`_SQL_DEFAULTS`, `COALESCE(:col, '<schema default>')`) and reference the raw bound
parameter (`:col`), not `excluded.col`, in the `ON CONFLICT` update clause — so "the
crawler didn't mention it" still means "leave the stored value alone" for every column.

- [ ] **Step 4: Write `seed.py`**

Twelve hand-written demo listings, three per metro, so search and the map are testable
before the crawler exists. `source="seed"`, `source_url="seed://…"` — they never collide
with a real crawl, and `our_description` is our own text (not scraped).

```python
"""Demo listings — three per metro. Lets search/map/rank be exercised (and tested)
before the crawler lands, and gives the README a keyless demo that touches no broker
site. `python -m app.seed` to (re)load."""
from .db import get_conn, init_db, save_listing

DEMO = [
    dict(metro="nyc", source="seed", source_url="seed://nyc/1", address="55 Gansevoort St, New York, NY",
         neighborhood="Meatpacking District", borough="Manhattan", lat=40.7392, lng=-74.0072,
         property_type="retail", transaction_type="lease", size_sf=2400, asking_rent=325, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=14.0, broker_firm="Demo Realty",
         our_description="Corner retail with 40 feet of frontage on a heavily trafficked cobblestone block."),
    dict(metro="nyc", source="seed", source_url="seed://nyc/2", address="1412 Broadway, New York, NY",
         neighborhood="Garment District", borough="Manhattan", lat=40.7538, lng=-73.9876,
         property_type="office", transaction_type="lease", size_sf=8200, asking_rent=62, rent_unit="sf_yr",
         lease_type="modified gross", floor="14", broker_firm="Demo Realty",
         our_description="Full-floor pre-built office one block from Bryant Park, wired and ready."),
    dict(metro="nyc", source="seed", source_url="seed://nyc/3", address="35-10 Astoria Blvd, Queens, NY",
         neighborhood="Astoria", borough="Queens", lat=40.7719, lng=-73.9196,
         property_type="industrial", transaction_type="lease", size_sf=15000, asking_rent=34, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=22.0, broker_firm="Demo Realty",
         our_description="Clear-span warehouse with drive-in loading, minutes from the Grand Central Parkway."),
    dict(metro="mia", source="seed", source_url="seed://mia/1", address="2618 NW 2nd Ave, Miami, FL",
         neighborhood="Wynwood", borough="Miami", lat=25.8015, lng=-80.1993,
         property_type="retail", transaction_type="lease", size_sf=1500, asking_rent=95, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Ground-floor retail in the middle of the Wynwood walls foot traffic."),
    dict(metro="mia", source="seed", source_url="seed://mia/2", address="1200 Brickell Ave, Miami, FL",
         neighborhood="Brickell", borough="Miami", lat=25.7601, lng=-80.1918,
         property_type="office", transaction_type="lease", size_sf=5400, asking_rent=78, rent_unit="sf_yr",
         lease_type="modified gross", floor="9", broker_firm="Demo Realty",
         our_description="Bay-view office suite in the core of Brickell's financial corridor."),
    dict(metro="mia", source="seed", source_url="seed://mia/3", address="7800 NW 25th St, Doral, FL",
         neighborhood="Doral", borough="Doral", lat=25.8000, lng=-80.3300,
         property_type="industrial", transaction_type="lease", size_sf=22000, asking_rent=16, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=28.0, broker_firm="Demo Realty",
         our_description="Airport-adjacent distribution space with dock-high loading."),
    dict(metro="la", source="seed", source_url="seed://la/1", address="8000 Melrose Ave, Los Angeles, CA",
         neighborhood="Melrose", borough="Hollywood", lat=34.0836, lng=-118.3639,
         property_type="retail", transaction_type="lease", size_sf=1800, asking_rent=72, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Boutique storefront on the Melrose shopping run, big glass line."),
    dict(metro="la", source="seed", source_url="seed://la/2", address="1100 S Flower St, Los Angeles, CA",
         neighborhood="South Park", borough="Downtown", lat=34.0407, lng=-118.2650,
         property_type="office", transaction_type="lease", size_sf=6100, asking_rent=44, rent_unit="sf_yr",
         lease_type="modified gross", floor="6", broker_firm="Demo Realty",
         our_description="Creative office loft with exposed brick, walking distance to the arena."),
    dict(metro="la", source="seed", source_url="seed://la/3", address="2500 E Vernon Ave, Vernon, CA",
         neighborhood="Vernon", borough="South Bay", lat=34.0033, lng=-118.2100,
         property_type="industrial", transaction_type="lease", size_sf=48000, asking_rent=19, rent_unit="sf_yr",
         lease_type="NNN", ceiling_height_ft=26.0, broker_firm="Demo Realty",
         our_description="Heavy-power manufacturing box in the Vernon industrial belt."),
    dict(metro="chi", source="seed", source_url="seed://chi/1", address="1550 N Damen Ave, Chicago, IL",
         neighborhood="Wicker Park", borough="Wicker Park", lat=41.9101, lng=-87.6773,
         property_type="retail", transaction_type="lease", size_sf=2100, asking_rent=58, rent_unit="sf_yr",
         lease_type="NNN", broker_firm="Demo Realty",
         our_description="Corner retail at the six-way, the busiest pedestrian node in Wicker Park."),
    dict(metro="chi", source="seed", source_url="seed://chi/2", address="222 W Merchandise Mart Plaza, Chicago, IL",
         neighborhood="River North", borough="River North", lat=41.8885, lng=-87.6354,
         property_type="office", transaction_type="lease", size_sf=12000, asking_rent=41, rent_unit="sf_yr",
         lease_type="gross", floor="12", broker_firm="Demo Realty",
         our_description="Tech-tenant floor in the Mart with river views and its own L stop."),
    dict(metro="chi", source="seed", source_url="seed://chi/3", address="4400 S Kildare Ave, Chicago, IL",
         neighborhood="Archer Heights", borough="Archer Heights", lat=41.8130, lng=-87.7310,
         property_type="industrial", transaction_type="sale", size_sf=31000, sale_price=2950000,
         ceiling_height_ft=24.0, broker_firm="Demo Realty",
         our_description="Owner-user industrial building for sale with rail-adjacent yard."),
]


def seed() -> int:
    init_db()
    for rec in DEMO:
        save_listing(rec)
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM listing").fetchone()["c"]


if __name__ == "__main__":
    print(f"seeded — {seed()} listings")
```

- [ ] **Step 5: Write the failing test**

`tests/test_models.py`:

```python
"""Models + the camelCase API boundary. The two copyright divergences (our_description
serialized AS `description`; photo_urls AS `photos`) are contract, so they are asserted."""
import json
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_models.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from app import db, seed  # noqa: E402
from app.models import METRO_KEYS, METROS, ListingQuery, to_api  # noqa: E402


def test_metros_loaded():
    assert METRO_KEYS == ("nyc", "mia", "la", "chi"), METRO_KEYS
    assert METROS["la"]["owner_available"] is False        # CA statute — not a bug
    assert METROS["chi"]["zoning_available"] == "partial"  # city-only
    assert len(METROS["nyc"]["bbox"]) == 4


def test_query_serializes_camel_case():
    q = ListingQuery(property_types=["retail"], min_size_sf=1000, max_rent_per_sf_yr=64.0)
    wire = q.model_dump(by_alias=True)
    assert wire["propertyTypes"] == ["retail"]
    assert wire["minSizeSf"] == 1000
    assert wire["maxRentPerSfYr"] == 64.0
    assert wire["transactionType"] == "lease"
    assert "excludeZip3" in wire and "excludeAddrStates" in wire
    # and it round-trips from the wire names (priorState comes back camelCase)
    assert ListingQuery(**{"propertyTypes": ["office"], "maxSizeSf": 5000}).max_size_sf == 5000


def test_upsert_and_api_shape():
    db.init_db()
    n = seed.seed()
    assert n == 12, n
    n_again = seed.seed()          # re-seed must UPDATE, not duplicate
    assert n_again == 12, n_again

    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM listing WHERE source_url = 'seed://mia/1'").fetchone()
    rid = row["id"]

    # a thinner re-crawl must not null out a good value (COALESCE guard)
    db.save_listing({"source_url": "seed://mia/1", "metro": "mia", "address": row["address"]})
    assert db.get_listing(rid)["size_sf"] == 1500

    api = to_api(db.get_listing(rid))
    assert api["sizeSf"] == 1500 and api["propertyType"] == "retail"
    assert api["transactionType"] == "lease" and api["sourceUrl"] == "seed://mia/1"
    # the two divergences: our prose is served under SpaceFinder's key; no `ourDescription`
    assert "Wynwood" in api["description"] and "ourDescription" not in api
    assert api["photos"] == []          # JSON columns decode, empty -> []
    assert "photoUrlsJson" not in api
```

- [ ] **Step 6: Run it — expect failure**

```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models'` (before Step 2 lands),
then `assert n == 12` once seeding works.

- [ ] **Step 7: Run to green**

```bash
.venv/bin/python -m pytest tests/test_models.py tests/test_smoke.py -v
```

Expected: `4 passed`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(openlease): Listing/ListingQuery/Parcel, metros.yml, upsert-by-source_url, camelCase API boundary"
```

---

### Task 3: Ranking — FTS5 BM25 + RRF

Keyless ranking, end to end. Cosine slots in at Task 12 **without touching this file's
callers**: RRF over one list is order-preserving, so keyless is not a special case.

**Files:**
- Create: `app/rank.py`
- Modify: `app/db.py` (append FTS5 table + sync triggers to `SCHEMA`)
- Test: `tests/test_rank.py`

**Interfaces:**
- Consumes: `db.get_conn`, `db.save_listing` (Task 2).
- Produces:
  - `rank.rrf(lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]`
  - `rank.bm25_ids(candidate_ids: list[int], keywords: list[str]) -> list[int]` — best first
  - `rank.rank_listings(candidate_ids: list[int], q: ListingQuery) -> list[dict]` — `[{"id", "score", "semantic_score", "rationale"}]`, best first, **every candidate present**

- [ ] **Step 1: Append the FTS5 table + triggers to `db.SCHEMA`**

External-content FTS5 (the text lives once, in `listing`). The triggers keep the index in
sync; the upsert in `save_listing` fires the UPDATE trigger, so a re-crawl reindexes.

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS listing_fts USING fts5(
    address, our_description, neighborhood,
    content='listing', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS listing_fts_ai AFTER INSERT ON listing BEGIN
    INSERT INTO listing_fts(rowid, address, our_description, neighborhood)
    VALUES (new.id, new.address, new.our_description, new.neighborhood);
END;
CREATE TRIGGER IF NOT EXISTS listing_fts_ad AFTER DELETE ON listing BEGIN
    INSERT INTO listing_fts(listing_fts, rowid, address, our_description, neighborhood)
    VALUES ('delete', old.id, old.address, old.our_description, old.neighborhood);
END;
CREATE TRIGGER IF NOT EXISTS listing_fts_au AFTER UPDATE ON listing BEGIN
    INSERT INTO listing_fts(listing_fts, rowid, address, our_description, neighborhood)
    VALUES ('delete', old.id, old.address, old.our_description, old.neighborhood);
    INSERT INTO listing_fts(rowid, address, our_description, neighborhood)
    VALUES (new.id, new.address, new.our_description, new.neighborhood);
END;
```

- [ ] **Step 2: Write `rank.py`**

```python
"""Hybrid ranking over the listings the hard filter already kept.

Three things here are load-bearing, all learned from the spec's research:

1. FTS5's `bm25()` is NEGATIVE (more negative = better), so it's `ORDER BY ... ASC`.
   Sorting DESC silently ranks the WORST matches first and everything still "works".
2. Raw prose in a `MATCH` throws on a stray apostrophe ("Macy's"). Tokenize, then quote
   each term.
3. Fuse with RRF (k=60), never a weighted sum: BM25 is unbounded and negative, cosine is
   [-1,1] — the scales are incomparable. RRF over ONE list is order-preserving, so the
   keyless path needs zero branching: it's the same call with one list in.
"""
import re

from .db import get_conn
from .models import ListingQuery

RRF_K = 60
_WORD = re.compile(r"[a-z0-9]+", re.I)


def match_expr(keywords: list[str]) -> str:
    """Keywords -> a safe FTS5 MATCH string. Each term is tokenized and double-quoted,
    so apostrophes/hyphens/punctuation can't blow up the query parser."""
    terms = []
    for kw in keywords:
        for tok in _WORD.findall(kw or ""):
            terms.append(f'"{tok}"')
    return " OR ".join(dict.fromkeys(terms))  # dedup, preserve order


def bm25_ids(candidate_ids: list[int], keywords: list[str]) -> list[int]:
    """Candidates ranked by BM25 relevance, best first. Candidates that match nothing
    are simply absent (rank_listings appends them after the ranked ones)."""
    expr = match_expr(keywords)
    if not expr or not candidate_ids:
        return []
    holes = ",".join("?" * len(candidate_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT rowid FROM listing_fts "
            f"WHERE listing_fts MATCH ? AND rowid IN ({holes}) "
            f"ORDER BY bm25(listing_fts) ASC",     # NEGATIVE score: ASC = best first
            [expr, *candidate_ids],
        ).fetchall()
    return [r["rowid"] for r in rows]


def rrf(lists: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion (Cormack/Clarke/Büttcher, SIGIR'09): score = Σ 1/(k+rank).
    With a single list the output order equals the input order — that is why the
    keyless (BM25-only) path is not a special case anywhere else in the app."""
    scores: dict[int, float] = {}
    for lst in lists:
        for i, id_ in enumerate(lst, start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + i)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _rationale(row: dict, q: ListingQuery) -> str:
    """One line: why this matched. Deterministic and keyless — the LLM writes the
    conversational reply, but every result explains itself even with no key."""
    bits = []
    if row.get("size_sf"):
        bits.append(f"{row['size_sf']:,} SF")
    if row.get("property_type"):
        bits.append(row["property_type"])
    if row.get("neighborhood"):
        bits.append(f"in {row['neighborhood']}")
    if row.get("asking_rent") and row.get("rent_unit") == "sf_yr":
        rent = f"${row['asking_rent']:,.0f}/SF/yr"
        if q.max_rent_per_sf_yr:
            rent += f" (under your ${q.max_rent_per_sf_yr:,.0f} cap)"
        bits.append("at " + rent)
    elif row.get("sale_price"):
        bits.append(f"asking ${row['sale_price']:,}")
    return " ".join(bits) or row.get("address", "")


def rank_listings(candidate_ids: list[int], q: ListingQuery) -> list[dict]:
    """Rank the survivors of the hard filter. Returns EVERY candidate — ranked ones
    first, then the rest in id order (a listing the filter kept is a valid answer even
    if it matched no keyword). Each carries SpaceFinder's three per-listing fields."""
    if not candidate_ids:
        return []
    lists = [ids for ids in (bm25_ids(candidate_ids, q.keywords),) if ids]
    # Task 12 appends the cosine list here; RRF's signature does not change.
    fused = rrf(lists) if lists else []

    ordered = [i for i, _ in fused]
    ordered += [i for i in candidate_ids if i not in set(ordered)]

    holes = ",".join("?" * len(candidate_ids))
    with get_conn() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM listing WHERE id IN ({holes})", candidate_ids
        ).fetchall()}

    n = len(ordered)
    out = []
    for i, id_ in enumerate(ordered):
        row = rows[id_]
        # semanticScore: 0..1 off the FUSED RANK (not the raw BM25 — that scale is
        # unbounded and negative, and means nothing to a client).
        sem = round(1.0 - (i / n), 3) if n > 1 else 1.0
        out.append({
            "id": id_,
            "semantic_score": sem,
            # ponytail: keyless, rank IS the whole signal, so score is sem*100. When a
            # second signal exists that isn't already inside the fusion, blend it here.
            "score": round(sem * 100, 1),
            "rationale": _rationale(row, q),
        })
    return out
```

- [ ] **Step 3: Write the failing test**

`tests/test_rank.py`:

```python
"""BM25 direction, RRF properties, and the keyless invariant (RRF over one list is a
passthrough — which is the whole reason the ranker has no `if voyage_key` branch)."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_rank.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

from app import db, rank  # noqa: E402
from app.models import ListingQuery  # noqa: E402


def _setup():
    db.init_db()
    with db.get_conn() as c:
        c.execute("DELETE FROM listing")
    ids = {}
    for slug, desc in [
        ("a", "Corner retail storefront in Wynwood with heavy foot traffic"),
        ("b", "Warehouse industrial space with dock loading"),
        ("c", "Wynwood retail retail retail gallery storefront"),
    ]:
        ids[slug] = db.save_listing(dict(
            metro="mia", source_url=f"t://{slug}", address=f"{slug} Test St",
            neighborhood="Wynwood" if slug != "b" else "Doral",
            property_type="retail" if slug != "b" else "industrial",
            size_sf=2000, asking_rent=80, rent_unit="sf_yr", our_description=desc,
        ))
    return ids


def test_bm25_is_ascending_best_first():
    ids = _setup()
    got = rank.bm25_ids(list(ids.values()), ["Wynwood", "retail"])
    # `c` mentions both terms more often -> best. `b` mentions neither -> absent.
    assert got and got[0] == ids["c"], got
    assert ids["b"] not in got, got


def test_match_expr_survives_punctuation():
    # raw prose in MATCH throws on an apostrophe; quoted tokens don't
    assert rank.match_expr(["Macy's", "co-working"]) == '"Macy" OR "s" OR "co" OR "working"'
    assert rank.match_expr([]) == "" and rank.match_expr(["!!!"]) == ""
    ids = _setup()
    assert rank.bm25_ids(list(ids.values()), ["Macy's"]) == []   # no throw, just no hits


def test_rrf_single_list_is_order_preserving():
    src = [7, 3, 9, 1]
    assert [i for i, _ in rank.rrf([src])] == src


def test_rrf_fuses_two_lists():
    # 9 sits at rank 2 in BOTH lists; 7 is rank 1 in one list but ABSENT from the
    # other (a list that never surfaced a candidate contributes nothing for it).
    # Agreement across lists beats a single list's top spot — a plain weighted sum
    # over one incomparable scale cannot express that.
    fused = [i for i, _ in rank.rrf([[7, 9, 3], [9, 3]])]
    assert fused[0] == 9 and set(fused) == {3, 7, 9}, fused
    scores = dict(rank.rrf([[7, 9, 3], [9, 3]]))
    assert scores[9] > scores[7]


def test_rrf_k_is_60_and_ranks_are_1_indexed():
    """The ordering assertions above hold for ANY k and either indexing base, so they
    cannot catch a regression in the two constants the spec calls load-bearing. Pin the
    arithmetic itself: rank-1 in a single list scores exactly 1/(60+1)."""
    assert rank.RRF_K == 60
    scores = dict(rank.rrf([[7, 3]]))
    assert abs(scores[7] - 1 / 61) < 1e-12, scores   # 1-indexed: 1/(60+1), not 1/(60+0)
    assert abs(scores[3] - 1 / 62) < 1e-12, scores


def test_rank_listings_returns_every_candidate():
    ids = _setup()
    q = ListingQuery(keywords=["Wynwood", "retail"], max_rent_per_sf_yr=100)
    out = rank.rank_listings(list(ids.values()), q)
    assert len(out) == 3, out                      # the unmatched `b` is still an answer
    assert out[0]["id"] == ids["c"]
    assert out[0]["semantic_score"] > out[-1]["semantic_score"]
    assert 0.0 <= out[-1]["semantic_score"] <= 1.0
    assert "SF" in out[0]["rationale"] and "under your" in out[0]["rationale"]
```

- [ ] **Step 4: Run it — expect failure**

```bash
.venv/bin/python -m pytest tests/test_rank.py -v
```

Expected: FAIL — `No module named 'app.rank'`. After `rank.py` lands, a stale DB may fail
on the missing FTS table; that is what Step 1 fixes.

- [ ] **Step 5: Confirm FTS5 is actually compiled in (it is — but prove it here, once)**

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
```

Expected: `FTS5 OK`. If this ever fails on a target machine, the ranker has no fallback —
that is deliberate (spec rejected `sqlite-vec` precisely because it is *not* universally
present; FTS5 is).

- [ ] **Step 6: Run to green**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(openlease): FTS5 BM25 (ASC — bm25 is negative) + RRF k=60 fusion, keyless-first ranker"
```

---

### Task 4: `ai.py` — NL → ListingQuery, with the sentinel schema

The schema rules here are the single most expensive lesson OpenProp learned. They are
reproduced verbatim, and the test enforces them so nobody "cleans them up" later.

**Files:**
- Create: `app/ai.py` (replaces the Task 1 stub entirely)
- Test: `tests/test_ai.py`

**Interfaces:**
- Consumes: `config.settings`, `models.ListingQuery`, `models.METROS` (Task 2).
- Produces:
  - `ai.available() -> bool`
  - `ai.nl_to_query(message: str, prior_state: dict | None, metro: str) -> ListingQuery`
  - `ai.QueryExtract` (the `messages.parse` schema — all-required sentinels)
  - `ai.reply(message: str, q: ListingQuery, results: list[dict], is_near_miss: bool, relaxed_what: str = "") -> tuple[str, list[str]]` — `(reply_text, suggestions)`; the ONE place the near-miss disclosure is composed (T6 review-fix pass — see `reply()`'s docstring below and `task-6-report.md`); keyless returns a deterministic summary
  - `ai._rules_parse(message: str, metro: str) -> ListingQuery` (the loudly-logged fallback)
  - `ai._drop_foreign_geo(prior: ListingQuery, metro: str) -> ListingQuery` (T6 review-fix pass: drops a prior turn's bbox/boroughs/neighborhood when they don't belong to the current metro — the defensive server-side half of the metro-switch fix, alongside the UI's own session reset in `home.html`)

- [ ] **Step 1: Write `ai.py`**

```python
"""AI layer — one BYO Anthropic key powers NL search, the conversational reply, LLM
extraction (crawl.py), listing descriptions, highlights, and per-listing chat.

With no key, `nl_to_query` falls back to a rules parser and `reply` to a deterministic
summary, so search still works — but the fallback understands FAR less of the query, so
it is LOUDLY logged. (A silent fallback hid a 400 for OpenProp's entire life and made
every AI search quietly drop half the user's constraints while still looking like it
worked.)

Both paid calls (`nl_to_query`'s `messages.parse`, `reply`'s `messages.create`) route
through `cache.cached()` — the only paid surfaces in the app, so this is the only place the
monthly budget cap (spec §6, §8) can be enforced. A refused-by-budget call is exactly
another degraded-mode fallback: it is LOUDLY logged, same as a parse failure."""
import logging
import re

from pydantic import BaseModel

from . import cache
from .config import settings
from .models import METROS, ListingQuery

log = logging.getLogger("openlease")

_TYPES = ("retail", "office", "industrial", "flex", "land")
_BBOX_FIELDS = ("min_lat", "max_lat", "min_lng", "max_lng")

# Anthropic pricing for the default `llm_model` (claude-opus-4-8): $5/1M input tokens,
# $25/1M output tokens (i.e. $0.0005c/input-tok, $0.0025c/output-tok).
#
# nl_to_query (messages.parse, max_tokens=1024): system prompt (~250 tok) + the QueryExtract
# schema definition sent with the request (~200 tok) + an optional prior-turn JSON blob on
# follow-ups (~100 tok) + the user's message (~50-150 tok) -> ~600-700 input tokens. The
# parsed-JSON output is normally 150-250 tokens (well under the 1024 cap).
#   ~700 * 0.0005c + ~250 * 0.0025c = 0.35c + 0.625c =~ 1c -> rounded up to 2c for headroom.
_PARSE_COST_CENTS = 2

# reply (messages.create, max_tokens=600): system prompt (~150 tok) + up to 8 listings'
# facts (~150 tok) + the user's message (~50 tok) -> ~350 input tokens. The reply is 2-3
# sentences plus 3 suggestions, normally 150-300 tokens (well under the 600 cap).
#   ~350 * 0.0005c + ~250 * 0.0025c = 0.175c + 0.625c =~ 1c -> rounded up to 2c for headroom.
_REPLY_COST_CENTS = 2


class QueryExtract(BaseModel):
    """The `messages.parse()` schema. Two rules are load-bearing, not style:

    1. NO `| None`. Structured outputs reject >16 union-typed (nullable) params
       ("too many parameters with union types ... limit: 16"). These fields as `X | None`
       are a hard 400.
    2. NO DEFAULTS — every field REQUIRED. A default makes a field *optional* in the JSON
       schema, and each optional field is a present/absent branch: N of them is 2^N shapes
       for the grammar compiler. The request doesn't 400 — it HANGS (>75s, times out) on
       every model, Haiku included. All-required = one shape = seconds.

    So: sentinels, not nulls. "" / 0 / [] mean "the query did not mention this," and are
    dropped in to_query() before they can become filters."""
    property_types: list[str]
    transaction_type: str          # "lease" | "sale" | "" (unmentioned -> lease)
    boroughs: list[str]
    neighborhood: str
    min_size_sf: int
    max_size_sf: int
    max_rent_per_sf_yr: float
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float
    exclude_addr_states: list[str]
    exclude_zip3: list[str]
    exclude_cities: list[str]
    keywords: list[str]            # free-text terms for BM25 (e.g. "corner", "loading dock")

    def to_query(self, *, default_transaction_type: str = "lease") -> ListingQuery:
        """Sentinels -> absent, so unmentioned fields never become real filters.

        Two fields get special handling instead of the flat per-field drop below:

        - The four bbox fields are ATOMIC: a real bounding box needs all four corners, so if
          even one is still at its 0/0.0 sentinel the WHOLE group is dropped. A partial bbox
          (e.g. a real minLat/maxLat/maxLng next to a sentinel minLng=0) is a geographically
          nonsensical filter that looks like it worked — worse than no bbox at all.
        - transaction_type resolves to `default_transaction_type` here rather than being
          dropped by the flat filter below. `nl_to_query` passes "" (not "lease") whenever
          there's a PRIOR turn to merge against, so the "unstated" sentinel survives into
          `_merge()` instead of being baked into a concrete "lease" before `_merge()` can
          tell "the user restated lease" apart from "the user didn't mention it" — which
          would otherwise silently flip a prior 'sale' search back to 'lease' on any
          follow-up that doesn't repeat the word.
        """
        dumped = self.model_dump()
        has_full_bbox = all(dumped[f] not in (0, 0.0) for f in _BBOX_FIELDS)
        # NOTE: `v not in (...)` uses `==`, and `False == 0` is True in Python — a bool field
        # added to this schema later would be silently dropped whenever it's False. No bool
        # fields exist today; if one is added, guard this with `type(v) is not bool and ...`.
        d = {
            k: v for k, v in dumped.items()
            if k not in _BBOX_FIELDS and k != "transaction_type"
            and v not in ("", 0, 0.0, [])
        }
        if has_full_bbox:
            d.update({f: dumped[f] for f in _BBOX_FIELDS})
        d["transaction_type"] = self.transaction_type or default_transaction_type
        return ListingQuery(**d)


def _client():
    import anthropic
    # bounded: the SDK default is 10min, so a bad schema/outage would freeze the request
    # instead of degrading to the rules parser.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=60.0)


def available() -> bool:
    return bool(settings.anthropic_api_key)


# --- NL -> ListingQuery -------------------------------------------------------

_SYSTEM = """Convert a commercial-real-estate tenant's plain-English space search into
structured filters for the {metro_name} market.

EVERY field is required. Use "" for text, 0 for numbers, [] for lists the query does not
mention. Never invent a constraint the user did not state.

Rules:
- propertyTypes from: retail, office, industrial, flex, land.
- transactionType is "sale" only if the user is buying; otherwise "lease".
- Rent is normalized to DOLLARS PER SF PER YEAR. Convert: a monthly total budget divided
  by the size, times 12. "under $8k/mo for ~1,500 SF" -> 8000 * 12 / 1500 = 64.
  A monthly per-SF rate ("$6/SF/mo") x 12. If no size is given, leave maxRentPerSfYr 0.
- A named neighborhood goes in `neighborhood` AND its bounding box in min/max lat/lng.
  Use the metro's own geography; the metro bbox is {bbox}.
- excludeCities: when the user names a city/neighborhood inside this metro, list the
  suburbs that would otherwise leak in.
- keywords: the qualitative terms worth text-matching ("corner", "high ceilings",
  "loading dock", "second generation"). Not the numbers — those are filters.
"""


def nl_to_query(message: str, prior_state: dict | None, metro: str) -> ListingQuery:
    """Parse `message` into filters. `prior_state` carries the PRIOR turn's mustHaves
    (camelCase, off the wire) so a follow-up refines instead of restarting: 'make it
    bigger, drop the rent cap' has to know what 'it' was.

    The `messages.parse` call is the paid step, so it goes through `cache.cached()` —
    identical repeated queries never re-bill, and a paid call past the monthly cap raises
    `BudgetExceeded` instead of silently spending. Either that or any other parse/API
    failure degrades to the rules parser, loudly logged (never silently)."""
    prior = ListingQuery(**prior_state) if prior_state else None
    if prior:
        prior = _drop_foreign_geo(prior, metro)
    # A fresh, no-prior search resolves an unstated transactionType to "lease" right here
    # (SpaceFinder's own default). A follow-up turn instead passes "" through unresolved, so
    # _merge() below can tell "the new turn restated lease" apart from "the new turn didn't
    # mention it" and keep the prior turn's own transaction_type (e.g. "sale") intact.
    default_txn = "" if prior else "lease"
    if not available():
        q = _rules_parse(message, metro, default_transaction_type=default_txn)
        return _merge(prior, q) if prior else q
    m = METROS.get(metro, {})
    req = {
        "message": message,
        "metro": metro,
        "prior": prior.model_dump(by_alias=True) if prior else None,
        "model": settings.llm_model,
    }

    def fetch():
        resp = _client().messages.parse(
            model=settings.llm_model, max_tokens=1024,
            system=_SYSTEM.format(metro_name=m.get("name", metro), bbox=m.get("bbox")),
            messages=[
                *([{"role": "user", "content": f"Prior search: {prior.model_dump_json(by_alias=True)}. "
                                               f"Refine it with the next message."}] if prior else []),
                {"role": "user", "content": message},
            ],
            output_format=QueryExtract,
        )
        return resp.parsed_output.model_dump()

    try:
        extracted = cache.cached("anthropic", "messages.parse", req, fetch, cost_cents=_PARSE_COST_CENTS)
        q = QueryExtract(**extracted).to_query(default_transaction_type=default_txn)
        return _merge(prior, q) if prior else q
    except cache.BudgetExceeded as e:
        log.warning(
            "AI query extraction skipped — monthly paid-spend cap reached (%s); falling "
            "back to the rules parser, which understands far less of the query.", e
        )
    except Exception as e:  # noqa: BLE001 — any parse/API failure degrades to rules
        log.warning(
            "AI query extraction failed (%s) — falling back to the rules parser, which "
            "understands far less of the query: %s", type(e).__name__, e
        )
    q = _rules_parse(message, metro, default_transaction_type=default_txn)
    return _merge(prior, q) if prior else q


def _drop_foreign_geo(prior: ListingQuery, metro: str) -> ListingQuery:
    """Defensive guard, independent of the UI's own metro-switch reset (T6 review-fix
    pass): a prior turn's neighborhood/bbox/boroughs were resolved against ITS metro's
    geography (a named neighborhood sets both `neighborhood` AND a full 4-corner bbox,
    per `_SYSTEM` above). If `metro` is now something else — the UI failed to reset
    session_id/prior_state, or a non-UI API client just never bothered to — blindly
    merging that geography in would silently intersect one city's coordinates against
    another's listings, a combination `filter_listings` can never satisfy. Worse,
    `_relax`'s "neighborhood" stage (see routes_search.py) clears
    `neighborhood`/`boroughs` but never the bbox, so the ladder can't rescue it either:
    every subsequent turn in the session would return zero rows, forever, with a message
    that hides the real cause. Drop the geography wholesale instead of merging it in as
    if it still applied."""
    meta = METROS.get(metro, {})
    bbox = meta.get("bbox")   # [min_lat, min_lng, max_lat, max_lng], per metros.yml
    has_bbox = all([prior.min_lat, prior.max_lat, prior.min_lng, prior.max_lng])
    bbox_is_foreign = has_bbox and bbox and not (
        bbox[0] <= prior.min_lat <= bbox[2] and bbox[0] <= prior.max_lat <= bbox[2] and
        bbox[1] <= prior.min_lng <= bbox[3] and bbox[1] <= prior.max_lng <= bbox[3]
    )
    boroughs_are_foreign = bool(prior.boroughs) and not any(
        b in meta.get("boroughs", []) for b in prior.boroughs
    )
    if not (bbox_is_foreign or boroughs_are_foreign):
        return prior
    return prior.model_copy(update={
        "min_lat": 0.0, "max_lat": 0.0, "min_lng": 0.0, "max_lng": 0.0,
        "boroughs": [], "neighborhood": "",
    })


def _merge(prior: ListingQuery, new: ListingQuery) -> ListingQuery:
    """Follow-up refinement: the new turn's stated fields win; unstated fields keep the
    prior turn's value. Sentinels mean 'unstated' for every field, including
    transaction_type — nl_to_query passes default_transaction_type="" (instead of resolving
    to "lease" before this runs) specifically so this dict update sees a real sentinel here
    too, not a concrete "lease" that would silently overwrite a prior 'sale' search."""
    base = prior.model_dump()
    for k, v in new.model_dump().items():
        if v not in ("", 0, 0.0, []):
            base[k] = v
    return ListingQuery(**base)


def _rules_parse(message: str, metro: str, *, default_transaction_type: str = "lease") -> ListingQuery:
    """Keyword fallback — covers the common tenant-rep phrasings. Deliberately dumb.

    `default_transaction_type` is what "the message didn't mention it" resolves to. A fresh
    search (no prior turn — see nl_to_query) defaults to "lease". A follow-up turn passes ""
    instead, so the unstated sentinel survives into `_merge()` rather than silently flipping
    a prior 'sale' search back to 'lease'."""
    q = message.lower()
    out = ListingQuery(transaction_type=default_transaction_type)
    out.property_types = [t for t in _TYPES if t in q]
    if "for sale" in q or "buy" in q or "purchase" in q:
        out.transaction_type = "sale"
    # size_hint is the user's actual stated number (e.g. the 1,500 in "~1,500 SF"). It's
    # kept separate from out.min/max_size_sf because the "~" branch below WIDENS those
    # into a range (1125/1875) for filtering purposes — but the rent-per-SF conversion
    # two blocks down needs the original figure the user typed, not the widened range,
    # or "$8k/mo for ~1,500 SF" silently converts against 1875 and comes out 51.2
    # instead of the correct 64.
    size_hint = 0
    if m := re.search(r"(?:under|below|less than|max|up to)\s*([\d,]+)\s*(?:sf|sq|square)", q):
        out.max_size_sf = int(m.group(1).replace(",", ""))
        size_hint = out.max_size_sf
    if m := re.search(r"(?:over|above|at least|min|minimum)\s*([\d,]+)\s*(?:sf|sq|square)", q):
        out.min_size_sf = int(m.group(1).replace(",", ""))
        size_hint = size_hint or out.min_size_sf
    if not out.min_size_sf and not out.max_size_sf:
        if m := re.search(r"([\d,]{3,})\s*(?:sf|sq ?ft|square feet)", q):   # "~1,500 SF"
            size_hint = int(m.group(1).replace(",", ""))
            out.min_size_sf, out.max_size_sf = int(size_hint * 0.75), int(size_hint * 1.25)
    # "$8k/mo" or "$8,000 a month" -> per-SF-per-year, but ONLY if we know the size
    if m := re.search(r"\$\s*([\d,.]+)\s*(k)?\s*(?:/|per |a )\s*mo", q):
        monthly = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        if size_hint:
            out.max_rent_per_sf_yr = round(monthly * 12 / size_hint, 2)
    elif m := re.search(r"\$\s*([\d,.]+)\s*(?:/|per )\s*(?:sf|psf)", q):
        out.max_rent_per_sf_yr = float(m.group(1).replace(",", ""))
    for hood in METROS.get(metro, {}).get("boroughs", []):
        if hood.lower() in q:
            out.boroughs = [hood]
            break
    # every noun the filters didn't consume is a text-match candidate
    stop = {"in", "a", "an", "the", "for", "with", "under", "over", "sf", "space", "need",
            "looking", "want", "around", "near", "about", "at", "to", "of", "and", "or"}
    out.keywords = [w for w in re.findall(r"[a-z][a-z-]{2,}", q) if w not in stop][:8]
    return out


# --- conversational reply -----------------------------------------------------

def reply(message: str, q: ListingQuery, results: list[dict], is_near_miss: bool,
          relaxed_what: str = "") -> tuple[str, list[str]]:
    """(reply, suggestions). Keyless: a deterministic summary. Keyed: the LLM writes it.

    T6 review-fix pass: `reply` is the ONE place the near-miss sentence gets composed —
    it is part of the JSON API contract (`POST /api/search`), read by non-UI clients too,
    so it must be SELF-CONTAINED: a caller reading only `reply` still has to learn a
    search was a near miss and exactly which of their stated constraints were dropped (a
    near-miss result VIOLATES something the user asked for, e.g. a $95/SF listing against
    a $64/SF cap — silently handing that back is worse than returning nothing).
    `routes_search.py` used to prepend this same sentence again on top of what this
    function already said, and the HTML banner said it a third time — so this function
    says it exactly once, and every other layer either stops repeating it
    (`routes_search.py`) or shrinks to a non-repeating label (the `_results.html`
    banner)."""
    if not results:
        return ("Nothing matches those constraints in this market yet. Try widening the "
                "size range or the rent cap.",
                ["Widen the size range", "Raise the rent cap", "Try a nearby neighborhood"])
    if not available():
        head = results[0]
        if is_near_miss:
            near = (f"Nothing matched exactly — I relaxed {relaxed_what}. "
                     if relaxed_what else "Nothing matched exactly, so here are the closest misses. ")
        else:
            near = ""
        return (f"{near}{len(results)} match{'es' if len(results) != 1 else ''}. "
                f"The closest is {head.get('address')} — {head.get('rationale', '')}.",
                ["Show only ground floor", "Raise the size cap", "Drop the rent cap"])
    facts = "\n".join(
        f"- {r.get('address')} | {r.get('sizeSf')} SF | {r.get('propertyType')} | "
        f"${r.get('askingRent')} {r.get('rentUnit')} | {r.get('rationale')}"
        for r in results[:8]
    )
    req = {
        "message": message, "is_near_miss": is_near_miss, "relaxed_what": relaxed_what,
        "facts": facts, "model": settings.llm_model,
    }

    def fetch():
        resp = _client().messages.create(
            model=settings.llm_model, max_tokens=600,
            system=("You are a commercial leasing broker replying to a tenant rep. In 2-3 "
                    "sentences, summarize what these listings offer against what they asked "
                    "for, and call out the single best fit by address. If isNearMiss is true, "
                    "open by saying plainly that nothing matched exactly and name exactly "
                    "what was relaxed (given below as relaxedWhat) — say it only ONCE, don't "
                    "repeat the disclosure later in the reply. Then give exactly 3 short "
                    "follow-up refinements, one per line, prefixed '- '. No preamble, no "
                    "markdown headers."),
            messages=[{"role": "user", "content":
                       f"They asked: {message}\nisNearMiss: {is_near_miss}\n"
                       f"relaxedWhat: {relaxed_what}\nMatches:\n{facts}"}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        lines = [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]
        body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("- ")).strip()
        return {"body": body, "lines": lines[:3]}

    try:
        out = cache.cached("anthropic", "messages.create", req, fetch, cost_cents=_REPLY_COST_CENTS)
        return out["body"], out["lines"]
    except cache.BudgetExceeded as e:
        log.warning(
            "AI reply skipped — monthly paid-spend cap reached (%s); returning the "
            "deterministic summary.", e
        )
    except Exception as e:  # noqa: BLE001
        log.warning("AI reply failed (%s) — returning the deterministic summary: %s",
                    type(e).__name__, e)
    settings_backup = settings.anthropic_api_key
    try:
        settings.anthropic_api_key = ""      # force the keyless branch, once
        return reply(message, q, results, is_near_miss, relaxed_what)
    finally:
        settings.anthropic_api_key = settings_backup


def demo() -> None:
    q = _rules_parse("retail in Wynwood ~1,500 SF under $8k/mo", "mia")
    assert q.property_types == ["retail"], q
    assert q.min_size_sf == 1125 and q.max_size_sf == 1875, q
    assert q.max_rent_per_sf_yr == 64.0, q.max_rent_per_sf_yr   # 8000*12/1500
    assert "wynwood" in " ".join(q.keywords), q.keywords

    # the schema rules that cost OpenProp its whole first life — enforced, not remembered
    for name, f in QueryExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> the request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"
    print("ai.demo (rules fallback + schema guards) OK")


if __name__ == "__main__":
    demo()
```

> **Correction (T6 review-fix pass, see `task-6-report.md`):** the block above already
> reflects two fixes made after Task 6 shipped, both to keep `reply()` and the
> near-miss-disclosure contract honest:
> 1. `reply()` gained a `relaxed_what: str = ""` parameter and now composes the ENTIRE
>    near-miss sentence itself, exactly once — it used to say only "Nothing matched
>    exactly, so here are the closest misses." (keyless) or rely on the LLM to guess
>    (keyed, which was never even told what was relaxed), while `routes_search.py`
>    prepended the same disclosure again and the `_results.html` banner said it a third
>    time. See Task 5's `api_search` (below) and Task 6's `_results.html` for the other
>    two sides of that fix.
> 2. `_drop_foreign_geo()` is new: a defensive guard so a prior turn's bbox/boroughs
>    (set when a keyed search names a neighborhood) can't survive into a follow-up turn
>    scoped to a DIFFERENT metro and silently zero out every result forever. The
>    required fix is on the UI side (`home.html` resets `session_id`/`prior_state` on a
>    metro change) — this is the belt-and-suspenders half for any caller that skips the UI.

- [ ] **Step 2: Write the failing test**

`tests/test_ai.py`:

```python
"""The rules fallback, the unit conversion, and the two schema rules. The schema
assertions are not paranoia: either mistake makes `messages.parse` 400 or HANG, the
caller silently falls back, and the AI search drops constraints the user typed.

Also covers the review-pass fixes on top of the original Task 4 implementation:
- the monthly budget cap actually gates the one paid surface, and a refused-by-budget
  call falls back loudly instead of crashing the search;
- a follow-up turn never silently flips a prior 'sale' search back to 'lease';
- a partial bbox is dropped as a whole atomic group instead of leaking through half-formed;
- the sentinel-drop test actually has detection power (the original version passed
  identically whether or not the sentinel-drop logic ran at all).
"""
import logging
import os

os.environ["ANTHROPIC_API_KEY"] = ""   # every test here runs the keyless path

from app import ai, db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import ListingQuery  # noqa: E402


def test_schema_is_all_required_and_non_nullable():
    for name, f in ai.QueryExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"


def test_sentinels_never_become_filters():
    """A realistic mix of stated and unstated fields. The original version of this test held
    identically whether or not to_query()'s sentinel-drop logic ran at all, because
    ListingQuery's own class defaults happen to equal the sentinel values for every field
    except transaction_type. This version adds real, non-default values (and a partial bbox)
    so the assertions have actual detection power -- confirmed by temporarily reverting
    to_query() to a bare `return ListingQuery(**self.model_dump())` passthrough and
    re-running: transaction_type comes back "" (not "lease") and the bbox comes back
    partially populated (not all-zero) -- this test fails either way with that reverted."""
    q = ai.QueryExtract(
        property_types=["retail"], transaction_type="", boroughs=[], neighborhood="Wynwood",
        min_size_sf=1000, max_size_sf=0, max_rent_per_sf_yr=64.0,
        min_lat=25.7, max_lat=25.8, min_lng=0, max_lng=-80.1,   # partial bbox: min_lng unstated
        exclude_addr_states=[], exclude_zip3=[], exclude_cities=["Hialeah"],
        keywords=["corner"],
    ).to_query()
    # real, stated values survive as real filters
    assert q.property_types == ["retail"]
    assert q.neighborhood == "Wynwood"
    assert q.min_size_sf == 1000
    assert q.max_rent_per_sf_yr == 64.0
    assert q.exclude_cities == ["Hialeah"]
    assert q.keywords == ["corner"]
    # unstated sentinels never become filters
    assert q.max_size_sf == 0
    assert q.transaction_type == "lease"      # the one sentinel with a real, non-empty default
    # the bbox is ATOMIC: 3 real corners + 1 sentinel corner drops the WHOLE group
    assert (q.min_lat, q.max_lat, q.min_lng, q.max_lng) == (0, 0, 0, 0)


def test_full_bbox_survives_when_all_four_corners_are_stated():
    """The bbox-atomicity fix must not zero out a genuinely complete bbox."""
    q = ai.QueryExtract(
        property_types=[], transaction_type="", boroughs=[], neighborhood="Wynwood",
        min_size_sf=0, max_size_sf=0, max_rent_per_sf_yr=0,
        min_lat=25.7, max_lat=25.8, min_lng=-80.2, max_lng=-80.1,
        exclude_addr_states=[], exclude_zip3=[], exclude_cities=[], keywords=[],
    ).to_query()
    assert (q.min_lat, q.max_lat, q.min_lng, q.max_lng) == (25.7, 25.8, -80.2, -80.1)


def test_rules_parse_monthly_budget_to_rent_per_sf_yr():
    q = ai._rules_parse("retail in Wynwood ~1,500 SF under $8k/mo", "mia")
    assert q.property_types == ["retail"]
    assert q.min_size_sf == 1125 and q.max_size_sf == 1875
    assert q.max_rent_per_sf_yr == 64.0          # 8000 * 12 / 1500
    assert q.transaction_type == "lease"


def test_rules_parse_sale_and_psf():
    q = ai._rules_parse("industrial building for sale under $30/sf", "chi")
    assert q.transaction_type == "sale" and q.max_rent_per_sf_yr == 30.0


def test_follow_up_refines_instead_of_restarting():
    prior = ListingQuery(property_types=["retail"], min_size_sf=1000, max_size_sf=2000,
                         max_rent_per_sf_yr=64.0)
    q = ai.nl_to_query("make it bigger — at least 5,000 sf", prior.model_dump(by_alias=True), "mia")
    assert q.min_size_sf == 5000              # the new constraint won
    assert q.property_types == ["retail"]     # the unstated one survived
    assert q.max_rent_per_sf_yr == 64.0


def test_follow_up_never_flips_a_sale_search_back_to_lease():
    """Reproduces the bug: a 'for sale' search, refined by a message that never mentions
    sale or lease, must NOT come back as 'lease'. Fails without the to_query()/
    _rules_parse()/_merge() fix -- the pre-fix code always came back "lease" here, because
    both to_query()'s setdefault and _rules_parse()'s ListingQuery() default resolved the ""
    sentinel to the concrete "lease" BEFORE _merge() ever ran, so _merge() had no way to
    tell "the user restated lease" apart from "the user didn't mention it"."""
    prior = ListingQuery(transaction_type="sale", property_types=["industrial"],
                         min_size_sf=10000, max_size_sf=20000)
    q = ai.nl_to_query("make it bigger, at least 25000 sf", prior.model_dump(by_alias=True), "mia")
    assert q.transaction_type == "sale"       # must survive the follow-up untouched
    assert q.min_size_sf == 25000             # the new constraint still won


def test_keyless_reply_is_deterministic_and_suggests():
    text, suggestions = ai.reply(
        "retail in wynwood", ListingQuery(),
        [{"address": "2618 NW 2nd Ave", "rationale": "1,500 SF retail in Wynwood"}], False,
    )
    assert "2618 NW 2nd Ave" in text and len(suggestions) == 3
    text, suggestions = ai.reply("x", ListingQuery(), [], False)
    assert "Nothing matches" in text and len(suggestions) == 3


# --- the monthly budget cap: paid calls route through cache.cached() ---------------------

def test_repeated_query_hits_cache_and_never_rebills(monkeypatch, tmp_path):
    """Never pay twice: an identical repeated query must not re-invoke the paid client."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_cache_hit.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 1000)
    calls = []

    class _FakeParsed:
        def model_dump(self):
            return {
                "property_types": ["retail"], "transaction_type": "", "boroughs": [],
                "neighborhood": "", "min_size_sf": 0, "max_size_sf": 0,
                "max_rent_per_sf_yr": 0, "min_lat": 0, "max_lat": 0, "min_lng": 0,
                "max_lng": 0, "exclude_addr_states": [], "exclude_zip3": [],
                "exclude_cities": [], "keywords": ["retail"],
            }

    class _FakeResp:
        parsed_output = _FakeParsed()

    class _FakeMessages:
        def parse(self, **kwargs):
            calls.append(1)
            return _FakeResp()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(ai, "_client", lambda: _FakeClient())

    ai.nl_to_query("retail space in wynwood", None, "mia")
    ai.nl_to_query("retail space in wynwood", None, "mia")   # identical query -> cache hit

    assert len(calls) == 1, "the second, identical query must be a cache hit, not a re-fetch"


def test_budget_exceeded_falls_back_to_rules_parser_and_logs_loudly(monkeypatch, tmp_path, caplog):
    """A paid call refused by the monthly budget must fall back to the rules parser (not
    crash the search) and must log LOUDLY at WARNING naming the budget as the reason --
    same as every other fallback (a silent fallback hid a 400 for OpenProp's entire life)."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)   # nothing left this month

    def _must_not_be_called():
        # deliberately avoids the word "budget" in this message -- the test's assertion
        # greps caplog for "budget", and that word must come from the real BudgetExceeded
        # path (cache.py's message names MONTHLY_BUDGET_CENTS), not leak in as a false
        # positive via this mock's own text if the code under test skips the cache/budget
        # check entirely and calls the client directly (the pre-fix bug).
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        q = ai.nl_to_query("retail space in wynwood", None, "mia")

    assert "budget" in caplog.text.lower()
    assert q.property_types == ["retail"]      # the rules parser still ran and understood this


def test_reply_budget_exceeded_falls_back_to_deterministic_summary(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ai_reply_budget.db"))
    db.init_db()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-key")
    monkeypatch.setattr(settings, "monthly_budget_cents", 0)

    def _must_not_be_called():
        # see the matching comment in test_budget_exceeded_falls_back_to_rules_parser_and_
        # logs_loudly -- deliberately avoids the word "budget" so the assertion below can
        # only pass via the real BudgetExceeded message, not a coincidental text match.
        raise AssertionError("the Anthropic client must not run when there is nothing left to spend")
    monkeypatch.setattr(ai, "_client", _must_not_be_called)

    with caplog.at_level(logging.WARNING, logger="openlease"):
        text, suggestions = ai.reply(
            "retail in wynwood", ListingQuery(),
            [{"address": "2618 NW 2nd Ave", "rationale": "1,500 SF retail in Wynwood"}], False,
        )

    assert "budget" in caplog.text.lower()
    assert "2618 NW 2nd Ave" in text and len(suggestions) == 3
```

- [ ] **Step 3: Run it — expect failure, then green**

```bash
.venv/bin/python -m pytest tests/test_ai.py -v
```

Expected first: FAIL (`app.ai` has only the stub `available()`). After `ai.py` lands: `11 passed`
(the original 6 plus 5 added in the review-pass fix documented below).

- [ ] **Step 4: Run the module self-check**

```bash
.venv/bin/python -m app.ai
```

Expected: `ai.demo (rules fallback + schema guards) OK`

- [ ] **Step 5: If a real key is available, prove the schema doesn't hang**

This is the one check that catches the failure the schema rules exist to prevent. Skip if
no key is at hand — the guards in Step 2 are the permanent net.

```bash
ANTHROPIC_API_KEY=sk-... .venv/bin/python -c "
import time; from app import ai
t=time.time(); q=ai.nl_to_query('retail in Wynwood around 1500 sf under \$8k a month', None, 'mia')
print(f'{time.time()-t:.1f}s', q.model_dump(by_alias=True))"
```

Expected: under ~10s (not >75s), `propertyTypes: ['retail']`, a nonzero `maxRentPerSfYr`,
and a Wynwood bbox. **If it hangs, a field went optional — that is the 2^N grammar bug.**

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(openlease): NL->ListingQuery via all-required sentinel schema + loudly-logged rules fallback"
```

> **Correction (review pass, follow-up commit):** a code review reproduced four defects in
> the block above — all four were plan-mandated (this file's own reference code had the bug),
> which does not excuse them: each violates a Global Constraint the plan itself declares.
>
> 1. **Anthropic calls bypassed the budget cap.** Neither `nl_to_query`'s `messages.parse` nor
>    `reply`'s `messages.create` referenced `cache.cached()` — the only paid surfaces in the
>    app never went through the monthly-budget guardrail (spec §6, §8), so `BudgetExceeded`
>    could never be raised and identical repeated queries re-billed every time. Fixed: both
>    calls now build a `fetch()` closure and go through
>    `cache.cached("anthropic", "messages.parse"/"messages.create", req, fetch, cost_cents=...)`
>    — `req` captures message/metro/prior/model (or message/is_near_miss/facts/model for
>    `reply`) so the cache key reflects everything that changes the answer. `cost_cents` is a
>    derived estimate (`_PARSE_COST_CENTS = _REPLY_COST_CENTS = 2`, derivation in the code
>    comment) since `cached()`'s budget check must run *before* the paid call, so it can't be
>    read off the real response. `cache.BudgetExceeded` is now caught explicitly and falls back
>    to the rules parser / deterministic summary with a WARNING naming the budget as the reason
>    — never a silent fallback, never a crash.
> 2. **A multi-turn 'for sale' search silently flipped back to 'lease'.** `to_query()`'s
>    `d.setdefault("transaction_type", "lease")` and `_rules_parse`'s `ListingQuery()` default
>    both collapsed the `""` sentinel into the concrete string `"lease"` *before* `_merge()`
>    ever ran, so `_merge()` had no way to tell "the user restated lease" apart from "the user
>    didn't mention it" — a follow-up that never mentions sale/lease silently overwrote a prior
>    `'sale'` search. Fixed: `to_query()` and `_rules_parse()` both take a keyword-only
>    `default_transaction_type` parameter; `nl_to_query` passes `""` whenever there's a PRIOR
>    turn to merge against (so the sentinel survives into `_merge()`) and `"lease"` only for a
>    fresh, no-prior search (where SpaceFinder's own default is correct immediately). `_merge`'s
>    `or (k == "transaction_type" and v)` clause was dead code (the outcome was already
>    unreachable given the bug) and is removed now that the sentinel is real.
> 3. **A partial bbox leaked through as a real filter.** `to_query()`'s flat
>    `v not in ("", 0, 0.0, [])` comprehension dropped `min_lat`/`max_lat`/`min_lng`/`max_lng`
>    *independently*, so 3 real coordinates + one still-0.0 sentinel produced a half-formed,
>    geographically nonsensical bbox that looked like it worked. Fixed: the four bbox fields
>    are now treated as an ATOMIC group — `to_query()` checks `all(... not in (0, 0.0) ...)`
>    across all four before including any of them; if even one is still a sentinel, the whole
>    group is dropped.
> 4. **`test_sentinels_never_become_filters` mostly passed for the wrong reason.** Two of its
>    three original assertions held identically whether or not `to_query()`'s sentinel filter
>    ran at all, because `ListingQuery`'s own class defaults happen to equal the sentinel
>    values for every field except `transaction_type`. Fixed: the test now exercises a
>    REALISTIC MIX of real values and sentinels (including a partial bbox), and asserts the
>    real ones survive as real filters while the sentinels — including the partial bbox —
>    never leak through. Verified by temporarily reverting `to_query()` to a bare
>    `ListingQuery(**self.model_dump())` passthrough: the rewritten test fails (transaction_type
>    comes back `""` instead of `"lease"`, and the bbox comes back partially populated instead
>    of all-zero); the original test would not have caught either regression.
>
> Also checked, per the review: `to_query()`'s `v not in (...)` filter uses `==`, and
> `False == 0` is `True` in Python — if a bool field is ever added to `QueryExtract`, `False`
> would be silently dropped as though it were the `0` sentinel. No bool fields exist today; the
> code carries a comment at the filter site flagging this for whoever adds one.
>
> `QueryExtract` stayed all-required throughout — none of the four fixes touch the schema's
> field definitions (only method bodies and their keyword-only parameters), so
> `test_schema_is_all_required_and_non_nullable` never had to change. Full corrected code (and
> the five added tests) is reflected verbatim in Step 1 and Step 2 above; see
> `.superpowers/sdd/task-4-report.md` → "Fix pass (review findings)" for the real RED→GREEN
> command output for each of the four fixes.

---

### Task 5: `POST /api/search` — the wire contract

The product, keyless, on seeded data. Request `{message, priorState, sessionId, metro}`,
response `{query, results[], reply, isNearMiss, suggestions[]}` — SpaceFinder's contract
verbatim, so a client written against theirs works against ours.

**Files:**
- Create: `app/routes_search.py`
- Modify: `app/db.py` (append `search_session`/`search_turn` to `SCHEMA`; add `filter_listings` + session helpers), `app/app.py` (import the route module)
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `ai.nl_to_query`, `ai.reply` (T4); `rank.rank_listings` (T3); `models.to_api`, `models.METRO_KEYS` (T2).
- Produces:
  - `db.filter_listings(q: ListingQuery, metro: str, limit: int = 200) -> list[dict]` — the **hard** filter; constraints are SQL `WHERE`, never soft-ranked away
  - `db.save_turn(session_id: str, metro: str, message: str, must_haves: dict, reply: str) -> None`
  - `db.list_sessions(limit: int = 20) -> list[dict]`, `db.get_session_turns(session_id: str) -> list[dict]`
  - `POST /api/search`, `GET /api/sessions`

- [ ] **Step 1: Append the session tables to `db.SCHEMA`**

```sql
CREATE TABLE IF NOT EXISTS search_session (
    id          TEXT PRIMARY KEY,      -- the client's sessionId
    metro       TEXT NOT NULL,
    title       TEXT,                  -- the first message, truncated — the "Recent" label
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS search_turn (
    id             INTEGER PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES search_session(id) ON DELETE CASCADE,
    message        TEXT NOT NULL,
    musthaves_json TEXT NOT NULL,      -- what priorState replays on the next turn
    reply          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON search_turn(session_id);
```

- [ ] **Step 2: Add the hard filter + session helpers to `db.py`**

```python
# --- the hard filter (spec Layer 3 step 2) ------------------------------------
# Every one of these is a CONSTRAINT, not a preference: it becomes SQL WHERE and is
# never soft-ranked away. Ranking happens over the survivors, in rank.py.

def filter_listings(q, metro: str, limit: int = 200) -> list[dict]:
    where = ["metro = ?", "status = 'available'"]
    args: list = [metro]

    if q.property_types:
        where.append(f"property_type IN ({','.join('?' * len(q.property_types))})")
        args += q.property_types
    if q.transaction_type:
        where.append("transaction_type = ?")
        args.append(q.transaction_type)
    if q.min_size_sf:
        # a divisible space qualifies if its SMALLEST split reaches the floor
        where.append("COALESCE(divisible_max_sf, size_sf) >= ?")
        args.append(q.min_size_sf)
    if q.max_size_sf:
        where.append("COALESCE(divisible_min_sf, size_sf) <= ?")
        args.append(q.max_size_sf)
    if q.max_rent_per_sf_yr:
        # only compare like units; a listing with no ask is NOT excluded by a rent cap
        where.append(
            "(asking_rent IS NULL OR ("
            "  CASE rent_unit"
            "    WHEN 'sf_yr' THEN asking_rent"
            "    WHEN 'sf_mo' THEN asking_rent * 12"
            "    WHEN 'mo'    THEN CASE WHEN size_sf > 0 THEN asking_rent * 12.0 / size_sf END"
            "  END) <= ?)"
        )
        args.append(q.max_rent_per_sf_yr)
    if q.min_lat and q.max_lat and q.min_lng and q.max_lng:
        where.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
        args += [q.min_lat, q.max_lat, q.min_lng, q.max_lng]
    if q.boroughs:
        where.append(f"borough IN ({','.join('?' * len(q.boroughs))})")
        args += q.boroughs
    if q.neighborhood:
        where.append("neighborhood LIKE ?")
        args.append(f"%{q.neighborhood}%")
    for col, vals in (("address", q.exclude_addr_states), ("neighborhood", q.exclude_cities)):
        for v in vals:                       # NOT-IN guards; excludes are hard too
            where.append(f"COALESCE({col}, '') NOT LIKE ?")
            args.append(f"%{v}%")
    for z3 in q.exclude_zip3:
        where.append("COALESCE(address, '') NOT LIKE ?")
        args.append(f"% {z3}%")

    sql = f"SELECT * FROM listing WHERE {' AND '.join(where)} LIMIT ?"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, [*args, limit]).fetchall()]


# --- search sessions ("Recent" history + priorState) --------------------------

def save_turn(session_id: str, metro: str, message: str, must_haves: dict, reply: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_session (id, metro, title) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (session_id, metro, message[:80]),
        )
        conn.execute(
            "INSERT INTO search_turn (session_id, message, musthaves_json, reply) "
            "VALUES (?, ?, ?, ?)",
            (session_id, message, json.dumps(must_haves), reply),
        )


def list_sessions(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.metro, s.title, s.created_at, COUNT(t.id) AS turns "
            "FROM search_session s LEFT JOIN search_turn t ON t.session_id = s.id "
            "GROUP BY s.id ORDER BY s.created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_turns(session_id: str) -> list[dict]:
    """Turns oldest-first, at the API boundary: the stored mustHaves is TEXT in the DB
    and an object on the wire. Returning the raw row would leak `musthaves_json` (a
    JSON string, snake_case) where the rest of the API serves `mustHaves` (an object)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT message, musthaves_json, reply, created_at FROM search_turn "
            "WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [
        {
            "message": r["message"],
            "mustHaves": json.loads(r["musthaves_json"]) if r["musthaves_json"] else {},
            "reply": r["reply"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]
```

- [ ] **Step 3: Write `routes_search.py`**

```python
"""POST /api/search — SpaceFinder's contract, verbatim.

  request  {message, priorState, sessionId, metro}
  response {query, results[], reply, isNearMiss, suggestions[]}

Pipeline (spec Layer 3): LLM parse -> HARD filter -> hybrid rank -> LLM reply.
Near-miss: when the hard filter returns nothing, relax the SOFTEST constraint (the rent
cap, then the size band) and re-run — but say so, rather than pretending the results
matched."""
import uuid

from fastapi import Depends, Request
from pydantic import BaseModel, Field

from . import ai, db, rank
from .app import app, require_auth
from .models import METRO_KEYS, ListingQuery, to_api


class SearchRequest(BaseModel):
    message: str
    priorState: dict | None = None     # the prior turn's query.mustHaves, camelCase
    sessionId: str | None = None
    metro: str = "nyc"


> **Correction (found during implementation, see task-5-report.md):** the version below
> re-derived "what to relax next" from the CURRENT state of `q`'s fields. That's fine for
> the rent-cap and neighborhood stages (each fully clears its own constraint in one shot),
> but the size stage only WIDENS — `min_size_sf` shrinks toward 0, `max_size_sf` only ever
> GROWS — so it never zeroes itself out. A single `while not rows:` loop that re-inspects
> field state therefore re-enters the "size" branch forever whenever widening size alone
> can never satisfy some OTHER hard constraint (e.g. `propertyTypes: ["land"]` with zero
> land inventory in any metro at any size) — an unbounded exponential widen that
> eventually raises `OverflowError: Python int too large to convert to SQLite INTEGER`
> from `db.filter_listings` (confirmed live: `POST /api/search {"message": "land in miami
> around 1500 sf", "metro": "mia"}` 500s). The fix: name the stage explicitly and have the
> caller iterate a fixed 3-stage ladder EXACTLY once each, so "at most once per stage" is
> a caller invariant instead of something inferred (and gotten wrong) from field state.
> The corrected shape:
>
> ```python
> _LADDER = (("rent", "the rent cap"), ("size", "the size range"),
>            ("neighborhood", "the neighborhood"))
>
>
> def _relax(q: ListingQuery, stage: str) -> ListingQuery | None:
>     """Apply ONE named stage. Returns None when this stage's constraint isn't set
>     (caller moves to the next stage) — see the correction note above for why the
>     stage must be named by the caller rather than inferred from field state."""
>     r = q.model_copy(deep=True)
>     if stage == "rent":
>         if not r.max_rent_per_sf_yr:
>             return None
>         r.max_rent_per_sf_yr = 0
>         return r
>     if stage == "size":
>         if not (r.min_size_sf or r.max_size_sf):
>             return None
>         r.min_size_sf = int(r.min_size_sf * 0.6) if r.min_size_sf else 0
>         r.max_size_sf = int(r.max_size_sf * 1.6) if r.max_size_sf else 0
>         return r
>     if stage == "neighborhood":
>         if not (r.neighborhood or r.boroughs):
>             return None
>         r.neighborhood, r.boroughs = "", []
>         return r
>     return None
> ```
>
> and in `api_search`, replace the `while not rows:` loop with:
>
> ```python
>     if not rows:
>         for stage, label in _LADDER:
>             step = _relax(q_used, stage)
>             if step is None:
>                 continue
>             q_used = step
>             candidate_rows = db.filter_listings(q_used, metro)
>             if candidate_rows:
>                 rows = candidate_rows
>                 relaxed_what = label
>                 is_near_miss = True
>                 break
> ```
>
> The pre-correction version below is kept for history; do not copy it as-is.

```python
def _relax(q: ListingQuery) -> tuple[ListingQuery, str] | None:
    """One step down the softness ladder. Returns None when nothing is left to relax."""
    r = q.model_copy(deep=True)
    if r.max_rent_per_sf_yr:
        r.max_rent_per_sf_yr = 0
        return r, "the rent cap"
    if r.min_size_sf or r.max_size_sf:
        r.min_size_sf = int(r.min_size_sf * 0.6) if r.min_size_sf else 0
        r.max_size_sf = int(r.max_size_sf * 1.6) if r.max_size_sf else 0
        return r, "the size range"
    if r.neighborhood or r.boroughs:
        r.neighborhood, r.boroughs = "", []
        return r, "the neighborhood"
    return None


@app.post("/api/search")
def api_search(body: SearchRequest, _=Depends(require_auth)):
    metro = body.metro if body.metro in METRO_KEYS else "nyc"
    session_id = body.sessionId or uuid.uuid4().hex

    q = ai.nl_to_query(body.message, body.priorState, metro)

    rows = db.filter_listings(q, metro)
    is_near_miss = False
    relaxed_what = ""
    q_used = q
    if not rows:
        # Each stage relaxes ON TOP of the prior one, and is tried EXACTLY once (see
        # _relax's docstring: re-deriving the next stage from field state re-enters "size"
        # forever, since max_size_sf only grows — an unbounded widen that overflows
        # SQLite's INTEGER bind).
        #
        # `applied` accumulates EVERY stage in force, not just the one that finally
        # produced rows. Reporting only the last is a half-truth: when the rent cap is
        # dropped and only the later size widening yields a hit, the results are still
        # uncapped on rent, and saying "I relaxed the size range" hands the user a listing
        # 95x over the ceiling they stated while claiming that ceiling held.
        applied: list[str] = []
        for stage, label in _LADDER:
            step = _relax(q_used, stage)
            if step is None:
                continue
            q_used = step
            applied.append(label)
            candidate_rows = db.filter_listings(q_used, metro)
            if candidate_rows:
                rows = candidate_rows
                relaxed_what = " and ".join(applied)
                is_near_miss = True
                break
        else:
            q_used = q      # ladder gave up: nothing matched, so nothing was relaxed

    ranked = rank.rank_listings([r["id"] for r in rows], q_used)
    by_id = {r["id"]: r for r in rows}
    results = []
    for r in ranked:
        item = to_api(by_id[r["id"]])
        item["semanticScore"] = r["semantic_score"]
        item["score"] = r["score"]
        item["rationale"] = r["rationale"]
        results.append(item)

    # ai.reply() is the ONE place that composes the near-miss sentence (T6 review-fix
    # pass — it's part of the JSON API contract, read by non-UI clients too, so it must
    # be self-contained). Do NOT re-prepend the disclosure here — that used to double it,
    # and the HTML banner tripled it on top. See ai.reply()'s docstring (Task 4).
    text, suggestions = ai.reply(body.message, q_used, results, is_near_miss, relaxed_what)

    must_haves = q.model_dump(by_alias=True)
    db.save_turn(session_id, metro, body.message, must_haves, text)

    return {
        "query": {"mustHaves": must_haves, "relaxed": relaxed_what or None},
        "results": results,
        "reply": text,
        "isNearMiss": is_near_miss,
        "suggestions": suggestions,
        "sessionId": session_id,
    }


@app.get("/api/sessions")
def api_sessions(_=Depends(require_auth)):
    return {"sessions": db.list_sessions()}


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str, _=Depends(require_auth)):
    return {"turns": db.get_session_turns(session_id)}
```

Add to the route-import block at the bottom of `app.py`:

```python
from . import routes_search     # noqa: E402,F401  (T5)
```

- [ ] **Step 4: Write the failing test**

`tests/test_search.py`:

```python
"""The wire contract, the hardness of the hard filter, and near-miss honesty. Keyless."""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_search.db")
os.environ["DB_PATH"] = _DB
os.environ["OPENLEASE_PASSWORD"] = "test-pw"
os.environ["ANTHROPIC_API_KEY"] = ""
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, seed  # noqa: E402
from app.app import app  # noqa: E402
from app.models import ListingQuery  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        yield c


def test_hard_filter_is_hard(client):
    # a rent cap below every seeded Wynwood ask must EXCLUDE, not merely down-rank
    q = ListingQuery(property_types=["retail"], max_rent_per_sf_yr=10)
    assert db.filter_listings(q, "mia") == []
    # a listing with no ask survives a rent cap (we don't punish missing data)
    db.save_listing(dict(metro="mia", source_url="t://noask", address="9 No Ask Ave",
                         property_type="retail", size_sf=1500, neighborhood="Wynwood"))
    assert [r["source_url"] for r in db.filter_listings(q, "mia")] == ["t://noask"]


def test_rent_unit_normalization(client):
    # $6/SF/MO is $72/SF/YR — it must fail a $64 cap, not pass it
    db.save_listing(dict(metro="chi", source_url="t://permo", address="1 Monthly St",
                         property_type="office", size_sf=1000, asking_rent=6, rent_unit="sf_mo"))
    q = ListingQuery(property_types=["office"], max_rent_per_sf_yr=64)
    assert "t://permo" not in [r["source_url"] for r in db.filter_listings(q, "chi")]
    q.max_rent_per_sf_yr = 80
    assert "t://permo" in [r["source_url"] for r in db.filter_listings(q, "chi")]


def test_search_contract_shape(client):
    r = client.post("/api/search", json={"message": "retail in wynwood around 1500 sf",
                                         "metro": "mia"})
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("query", "results", "reply", "isNearMiss", "suggestions", "sessionId"):
        assert k in body, (k, body.keys())
    assert "mustHaves" in body["query"]
    assert body["query"]["mustHaves"]["propertyTypes"] == ["retail"]
    hit = body["results"][0]
    for k in ("sizeSf", "propertyType", "description", "photos", "semanticScore",
              "score", "rationale", "sourceUrl"):
        assert k in hit, (k, sorted(hit))
    assert "Wynwood" in hit["description"]     # OUR prose, under SpaceFinder's key


def test_near_miss_relaxes_and_says_so(client):
    r = client.post("/api/search", json={
        "message": "office around 8000 sf under $1/sf", "metro": "nyc"})
    body = r.json()
    assert body["isNearMiss"] is True, body["reply"]
    assert body["results"], "relaxation should have found the near misses"
    assert "relaxed" in body["reply"].lower()
    assert body["query"]["relaxed"] == "the rent cap"


def test_session_history_and_prior_state(client):
    r1 = client.post("/api/search", json={"message": "retail in wynwood 1500 sf",
                                          "metro": "mia"}).json()
    sid = r1["sessionId"]
    r2 = client.post("/api/search", json={
        "message": "make it bigger — at least 5000 sf", "metro": "mia",
        "sessionId": sid, "priorState": r1["query"]["mustHaves"]}).json()
    assert r2["query"]["mustHaves"]["minSizeSf"] == 5000
    assert r2["query"]["mustHaves"]["propertyTypes"] == ["retail"]   # carried forward
    sessions = client.get("/api/sessions").json()["sessions"]
    assert any(s["id"] == sid and s["turns"] == 2 for s in sessions), sessions
```

- [ ] **Step 5: Run — expect failure, then green**

```bash
.venv/bin/python -m pytest tests/test_search.py -v
```

Expected first: FAIL — `No module named 'app.routes_search'`. Then `5 passed`.

> **Correction:** a 6th test, `test_near_miss_ladder_terminates_when_nothing_helps`, was
> added during implementation as the regression test for the `_relax` bug above (a
> `propertyTypes: ["land"]` search — zero land inventory in any seeded metro at any size —
> used to 500 with `OverflowError` as the softness ladder re-widened `maxSizeSf`
> unboundedly; it must instead exhaust the ladder and answer honestly: `results: []`,
> `isNearMiss: false`). Expected after the fix: `6 passed`.
>
> **Correction (T6 review-fix pass, see `task-6-report.md`):** three more tests have
> since landed in `tests/test_search.py`, bringing it to 9:
> `test_near_miss_discloses_every_constraint_it_dropped` (the ladder relaxes
> cumulatively — the disclosure must name every stage actually in force, not just the
> last one that produced rows);
> `test_near_miss_reply_names_relaxed_constraint_exactly_once` (asserts
> `body["reply"].lower().count("nothing matched exactly") == 1` — regression test for the
> doubled/tripled near-miss sentence, see `ai.reply()`'s docstring above); and
> `test_metro_switch_drops_stale_geographic_constraint` (regression test for
> `ai._drop_foreign_geo` — a stale Miami bbox in `priorState` must not poison a follow-up
> scoped to NYC). Expected: `9 passed`.

- [ ] **Step 6: Exercise it by hand**

```bash
.venv/bin/python -m app.seed && ./run.sh &
sleep 3
curl -s -c /tmp/ol.jar -X POST localhost:8788/login -d 'password=changeme' -o /dev/null
curl -s -b /tmp/ol.jar -X POST localhost:8788/api/search -H 'content-type: application/json' \
  -d '{"message":"retail in Wynwood around 1500 sf under $8k/mo","metro":"mia"}' | head -c 600
kill %1
```

Expected: a JSON body whose `results[0].address` is `2618 NW 2nd Ave, Miami, FL`.

> **Correction:** this address does come back, but NOT as a direct hit — read literally,
> "$8k/mo" for "~1,500 SF" parses to a `maxRentPerSfYr` cap of `8000*12/1500 = 64`. The
> seeded Wynwood listing's actual ask is `$95/SF/yr` (set in `app/seed.py` since Task 2,
> unchanged), which is ABOVE that cap. So the first (unrelaxed) filter pass correctly
> returns zero rows — the hard filter is doing its job — and the near-miss ladder relaxes
> the rent cap to find this listing. The real response has `isNearMiss: true` and
> `query.relaxed: "the rent cap"`. This is still a fair hand-check (the address matches,
> and it now *also* demonstrates the near-miss path on real seeded data), but the original
> wording implied a direct match; it does not happen with this specific rent figure.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(openlease): POST /api/search — LLM parse -> hard filter -> RRF rank -> reply, with honest near-miss"
```

---

### Task 6: The UI — metro switcher, chat search, MapLibre map, result cards, listing page

Everything the broker touches. HTMX posts to a thin HTML endpoint that reuses the same
pipeline as `/api/search`, so there is one search path, not two.

**Files:**
- Create: `app/routes_listings.py`
- Modify: `app/templates/home.html` (replace the placeholder), `app/app.py`
- Create: `app/templates/_results.html`, `_listing_card.html`, `listing.html`
- Test: extend `tests/test_smoke.py`

**Interfaces:**
- Consumes: everything from T5.
- Produces: `GET /` (search UI), `POST /search` (HTMX fragment), `GET /listings/{id}` (detail page), `GET /api/listings/{id}` (enriched JSON).

- [ ] **Step 1: Write `routes_listings.py`**

```python
"""The HTML surface. `/search` is the HTMX twin of `/api/search` — same pipeline, one
call, so the two can never drift."""
import json

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db
from .app import app, require_auth, spend_ctx, templates
from .models import METROS, to_api
from .routes_search import SearchRequest, api_search


@app.post("/search", response_class=HTMLResponse)
def search_fragment(request: Request, message: str = Form(...), metro: str = Form("nyc"),
                    session_id: str = Form(""), prior_state: str = Form(""),
                    _=Depends(require_auth)):
    body = SearchRequest(
        message=message, metro=metro,
        sessionId=session_id or None,
        priorState=json.loads(prior_state) if prior_state else None,
    )
    res = api_search(body, True)
    return templates.TemplateResponse(request, "_results.html", res)


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(request: Request, listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    if not row:
        return HTMLResponse("<p class='p-6'>No such listing.</p>", status_code=404)
    return templates.TemplateResponse(
        request, "listing.html",
        {"l": to_api(row), "metro_meta": METROS[row["metro"]],
         "parcel": None, **spend_ctx()},   # T9 fills `parcel`
    )


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    return to_api(row) if row else {"error": "not found"}
```

> **Correction (T6):** the block above is written in Starlette's current
> `TemplateResponse(request, name, context)` form, not the old
> `TemplateResponse(name, {"request": request, ...})` shape the first draft of this step had.
> With `-W error` (the project's zero-warnings bar) the old shape fails the suite outright — see
> the same correction already made for `routes_settings.py`/`login`/`home` at line ~507. `import
> json` also moves to the module top (it was a stray function-local import serving no purpose —
> `json.loads` is only called once, at module scope's natural cost).

Add to `app.py`'s route-import block:

```python
from . import routes_listings   # noqa: E402,F401  (T6)
```

- [ ] **Step 2: Replace `templates/home.html`**

Map + chat + results, all on one screen. The map is fed from the results fragment via a
tiny `hx-on` hook — no build step, no framework.

```html
{% extends "base.html" %}
{% block content %}
<div class="grid grid-cols-1 lg:grid-cols-2 gap-4" style="height: calc(100vh - 8rem)">
  <div class="flex flex-col min-h-0">
    <form hx-post="/search" hx-target="#results" hx-indicator="#spin"
          class="flex gap-2 items-center mb-3">
      <!-- a metro change starts a NEW search: without this, a stale sessionId/priorState
           from the old metro (e.g. a neighborhood-derived bbox) rides into the new
           metro's turn and can zero out every result forever (see ai._drop_foreign_geo
           for the matching server-side guard) -->
      <select name="metro" class="rounded border-slate-300 text-sm py-2"
              onchange="document.getElementById('session_id').value='';
                        document.getElementById('prior_state').value='';">
        {% for key, m in metros.items() %}
        <option value="{{ key }}" {% if key == metro %}selected{% endif %}>{{ m.name }}</option>
        {% endfor %}
      </select>
      <input name="message" required autofocus
             placeholder="retail in Wynwood ~1,500 SF under $8k/mo"
             class="flex-1 rounded border-slate-300 text-sm py-2">
      <input type="hidden" name="session_id" id="session_id" value="">
      <input type="hidden" name="prior_state" id="prior_state" value="">
      <button class="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white">Search</button>
      <span id="spin" class="htmx-indicator text-xs text-slate-400">…</span>
    </form>
    <div id="results" class="overflow-y-auto flex-1 pr-1">
      <p class="text-sm text-slate-500">Describe the space you need — plain English.</p>
    </div>
  </div>
  <div id="map" class="rounded-lg border bg-white min-h-0"></div>
</div>

<script>
  const CENTER = {{ metros[metro].center | tojson }};
  const map = new maplibregl.Map({
    container: 'map',
    style: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
    center: [CENTER[1], CENTER[0]], zoom: 11,
  });
  let markers = [];
  // the results fragment carries the pins as JSON; draw them when HTMX swaps it in
  document.body.addEventListener('htmx:afterSwap', (e) => {
    if (e.target.id !== 'results') return;
    const data = document.getElementById('pins');
    if (!data) return;
    markers.forEach(m => m.remove()); markers = [];
    const pins = JSON.parse(data.textContent);
    document.getElementById('session_id').value = data.dataset.session || '';
    document.getElementById('prior_state').value = data.dataset.must || '';
    const b = new maplibregl.LngLatBounds();
    pins.forEach(p => {
      if (p.lng == null || p.lat == null) return;
      const el = document.createElement('div');
      el.className = 'rounded-full bg-sky-600 text-white text-[10px] px-2 py-1 shadow cursor-pointer';
      el.textContent = p.label;
      el.onclick = () => location.href = '/listings/' + p.id;
      markers.push(new maplibregl.Marker({element: el}).setLngLat([p.lng, p.lat]).addTo(map));
      b.extend([p.lng, p.lat]);
    });
    if (!b.isEmpty()) map.fitBounds(b, {padding: 60, maxZoom: 15});
  });
</script>
{% endblock %}
```

`home()` in `app.py` must now pass `metros`, in the same current-signature form as every
other `TemplateResponse` call in this file (request first, no `request` key in the context dict):

```python
@app.get("/", response_class=HTMLResponse)
def home(request: Request, _=Depends(require_auth)):
    from .models import METROS
    return templates.TemplateResponse(
        request, "home.html", {"metro": "nyc", "metros": METROS, **spend_ctx()}
    )
```

- [ ] **Step 3: Write `templates/_results.html` and `_listing_card.html`**

`_results.html`:

```html
<div class="mb-3 rounded-lg bg-white border p-3 text-sm">
  {% if isNearMiss %}
  {# `reply` below already discloses what was relaxed, in full, exactly once (ai.reply()
     is the one place that composes that sentence) -- this badge is a short label, not a
     second telling of the same sentence. #}
  <p class="mb-1 text-xs font-medium text-amber-700">Near miss</p>
  {% endif %}
  <p class="whitespace-pre-line">{{ reply }}</p>
  {% if suggestions %}
  <div class="mt-2 flex flex-wrap gap-2">
    {% for s in suggestions %}
    <button class="rounded-full border px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
            onclick="document.querySelector('[name=message]').value = {{ s|tojson }};
                     htmx.trigger(document.querySelector('form'), 'submit')">{{ s }}</button>
    {% endfor %}
  </div>
  {% endif %}
</div>

<p class="mb-2 text-xs text-slate-500">{{ results|length }} listing{{ '' if results|length == 1 else 's' }}</p>
{% for l in results %}{% include "_listing_card.html" %}{% endfor %}

<script id="pins" type="application/json"
        data-session="{{ sessionId }}"
        data-must="{{ query.mustHaves | tojson | forceescape }}">
[{% for l in results %}{"id": {{ l.id }}, "lat": {{ l.lat if l.lat is not none else 'null' }}, "lng": {{ l.lng if l.lng is not none else 'null' }},
  "label": {{ ((l.sizeSf|string ~ ' SF') if l.sizeSf else l.address) | tojson }}}{{ "," if not loop.last }}{% endfor %}]
</script>
```

(A listing we couldn't geocode emits `null` and the JS skips it — it's still a valid search
result, it just has no pin.)

`_listing_card.html`:

```html
<a href="/listings/{{ l.id }}"
   class="block mb-2 rounded-lg border bg-white p-3 hover:border-sky-400">
  <div class="flex items-start justify-between gap-3">
    <div>
      <p class="font-medium text-sm">{{ l.address }}</p>
      <p class="text-xs text-slate-500">
        {{ l.neighborhood or l.borough or '' }}
        {% if l.propertyType %}· {{ l.propertyType }}{% endif %}
        {% if l.sizeSf %}· {{ "{:,}".format(l.sizeSf) }} SF{% endif %}
      </p>
      <p class="mt-1 text-xs text-slate-600">{{ l.rationale }}</p>
    </div>
    <div class="text-right shrink-0">
      {% if l.askingRent is not none %}
      <p class="text-sm font-semibold">${{ "{:,.0f}".format(l.askingRent) }}
        <span class="text-xs font-normal text-slate-400">/SF/yr</span></p>
      {% elif l.salePrice is not none %}
      <p class="text-sm font-semibold">${{ "{:,}".format(l.salePrice) }}</p>
      {% else %}
      <p class="text-xs text-slate-400">ask on request</p>
      {% endif %}
      {% if l.walkScore is not none %}<p class="text-[10px] text-slate-400">walk {{ l.walkScore }}</p>{% endif %}
    </div>
  </div>
</a>
```

> **Correction (T6 review-fix pass, see `task-6-report.md`):** both blocks above already
> show the fixed form. `_results.html`'s near-miss banner used to read "Near miss —
> nothing matched exactly." — a full sentence that duplicated `ai.reply()`'s own
> disclosure (and, combined with `routes_search.py`'s now-removed prepend, said the same
> thing THREE times on one screen); it is now a short "Near miss" label. `_results.html`'s
> pin JSON used `{{ l.lat or 'null' }}` / `{{ l.lng or 'null' }}`, which would treat a real
> `lat=0.0`/`lng=0.0` as "no coordinates" (theoretical for these four metros, but the same
> bug class as the one below); both now check `is not none`. `_listing_card.html`'s
> `{% if l.askingRent %}` / `{% if l.walkScore %}` would silently hide a legitimate
> `walkScore` of `0` ("car-dependent", a real Walk Score, landing in Task 8) — both now
> check `is not none`, matching `listing.html`'s own pattern below.

- [ ] **Step 4: Write `templates/listing.html`**

The `null`-with-a-reason contract is a UI element here, not a comment: a field the metro
does not publish says so.

```html
{% extends "base.html" %}
{% block title %}{{ l.address }} — OpenLease{% endblock %}
{% block content %}
{# seed:// is a synthetic marker for demo listings with no broker page -- suppressing
   the link there is correct. The footnote below must agree with this same test in BOTH
   directions: it may only promise "follow the link above" when the link actually rendered. #}
{% set has_source_link = l.sourceUrl and not l.sourceUrl.startswith('seed://') %}
<a href="/" class="text-xs text-slate-400 hover:text-slate-700">&larr; back to search</a>
<h1 class="mt-2 text-xl font-semibold">{{ l.address }}</h1>
<p class="text-sm text-slate-500">
  {{ l.neighborhood or '' }}{% if l.borough %}, {{ l.borough }}{% endif %}
  · {{ metro_meta.name }}
  {% if has_source_link %}
  · <a class="text-sky-600 hover:underline" href="{{ l.sourceUrl }}" target="_blank" rel="noopener">
      original listing &nearr;</a>
  {% endif %}
</p>

<div class="mt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
  <div class="md:col-span-2 space-y-4">
    <div class="rounded-lg border bg-white p-4">
      <dl class="grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm">
        {% for label, val in [
            ("Type", l.propertyType), ("Size", "{:,} SF".format(l.sizeSf) if l.sizeSf else None),
            ("Ask", "${:,.0f}/SF/yr".format(l.askingRent) if l.askingRent else
                    ("${:,}".format(l.salePrice) if l.salePrice else None)),
            ("Lease type", l.leaseType), ("Floor", l.floor),
            ("Ceiling", "{} ft".format(l.ceilingHeightFt) if l.ceilingHeightFt else None),
            ("Available", l.availabilityDate), ("Walk Score", l.walkScore),
            ("Transit Score", l.transitScore)] %}
          {% if val is not none %}
          <div><dt class="text-xs text-slate-400">{{ label }}</dt><dd class="font-medium">{{ val }}</dd></div>
          {% endif %}
        {% endfor %}
      </dl>
    </div>

    {% if l.description %}
    <div class="rounded-lg border bg-white p-4">
      <h2 class="text-sm font-semibold mb-1">About the property</h2>
      <p class="text-sm text-slate-700">{{ l.description }}</p>
      <p class="mt-2 text-[10px] text-slate-400">
        {% if has_source_link %}
        Written by OpenLease from the listing's facts. The broker's own copy and photos stay
        on their site — follow the link above.
        {% else %}
        Written by OpenLease from the listing's facts. This listing has no broker page to
        link to.
        {% endif %}</p>
    </div>
    {% endif %}

    {% if l.photos %}
    <div class="grid grid-cols-3 gap-2">
      {% for p in l.photos %}
      <img src="{{ p }}" referrerpolicy="no-referrer" loading="lazy"
           class="rounded border object-cover h-28 w-full" alt="">
      {% endfor %}
    </div>
    {% endif %}

    {% block listing_extra %}{% endblock %}  {# T8 score breakdown, T13 chat #}
  </div>

  <aside class="space-y-4">
    <div class="rounded-lg border bg-white p-4 text-sm">
      <h2 class="text-sm font-semibold mb-2">Parcel</h2>
      {% if parcel %}
      <dl class="space-y-2">
        {% for label, key in [("Owner", "owner_name"), ("Zoning", "zoning"),
                              ("Year built", "year_built"), ("Lot", "lot_sqft"),
                              ("Building", "bldg_sqft"), ("Floors", "floors")] %}
        <div class="flex justify-between gap-3">
          <dt class="text-xs text-slate-400">{{ label }}</dt>
          {% if parcel[key] is not none %}
          <dd class="font-medium">{{ parcel[key] }}</dd>
          {% else %}
          <dd class="text-xs text-slate-400 italic text-right"
              title="{{ parcel.missing_reason.get(key, 'not published in this market') }}">
            not published here</dd>
          {% endif %}
        </div>
        {% endfor %}
      </dl>
      {% else %}
      <p class="text-xs text-slate-400">No parcel matched this address.</p>
      {% endif %}
    </div>

    {% if l.brokerName or l.brokerFirm %}
    <div class="rounded-lg border bg-white p-4 text-sm">
      <h2 class="text-sm font-semibold mb-1">Broker</h2>
      <p>{{ l.brokerName or '' }}</p>
      <p class="text-xs text-slate-500">{{ l.brokerFirm or '' }}</p>
      {% if l.brokerPhone %}<p class="text-xs">{{ l.brokerPhone }}</p>{% endif %}
    </div>
    {% endif %}
  </aside>
</div>
{% endblock %}
```

> **Correction (T6 review-fix pass, see `task-6-report.md`):** the footnote used to
> unconditionally read "...The broker's own copy and photos stay on their site — follow
> the link above," pointing at a link that only rendered when `has_source_link` was true.
> Every listing viewable before Task 9/10 land is seed data (`source_url` starting
> `seed://`), so the page always lied. The block above now agrees with `has_source_link`
> in both directions.

- [ ] **Step 5: Extend `tests/test_smoke.py`**

Append to the existing file:

```python
def test_search_ui_and_listing_page():
    from app import db, seed
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

        # the listing page renders our prose and never re-hosts. seed data is
        # source_url='seed://...' -- a synthetic marker meaning "no broker page exists" --
        # so the "original listing" link and its footnote must be CORRECTLY ABSENT here.
        # The case where a real http(s) source_url DOES get linked is covered by
        # test_listing_page_links_real_broker_source_url below.
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://mia/1'").fetchone()["id"]
        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert "About the property" in r.text and "Wynwood" in r.text
        assert "original listing" not in r.text
        assert "follow the link above" not in r.text


def test_listing_page_links_real_broker_source_url():
    # The spec's "always link sourceUrl" rule, for the one case that matters for a live
    # crawl: a real http(s) source_url. The link must be rendered AND the footnote must
    # point at it -- unlike the seed:// case above, where both are correctly absent.
    from app import db
    with TestClient(app, follow_redirects=False) as c:
        db.init_db()
        c.post("/login", data={"password": "test-pw"})
        lid = db.save_listing(dict(
            metro="mia", source_url="https://broker.example.com/listings/42",
            address="42 Real Broker Ave, Miami, FL", property_type="retail", size_sf=1000,
            our_description="Ground-floor retail near the broker's own listing page.",
        ))
        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert 'href="https://broker.example.com/listings/42"' in r.text
        assert "original listing" in r.text
        assert "follow the link above" in r.text


def test_new_routes_require_auth():
    # /search, /listings/{id}, /api/listings/{id} all landed in Task 6 -- none may leak a
    # 200 to an unauthenticated caller.
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/search", data={"message": "x", "metro": "nyc"}).status_code != 200
        assert c.get("/listings/1").status_code != 200
        assert c.get("/api/listings/1").status_code != 200
```

> **Correction (T6 review-fix pass, see `task-6-report.md`):** the original assertion
> `assert "The broker's own copy and photos stay" in r.text` no longer holds for the
> seed:// case above once the footnote fix lands (that text is now ONLY rendered when a
> real link is rendered) — updated to assert the opposite, and its comment corrected (the
> original comment claimed this test "links the original," but seed:// is precisely the
> case where the link is CORRECTLY absent). Two more tests were added:
> `test_listing_page_links_real_broker_source_url` (the case the original test's comment
> claimed to cover but didn't — a real http(s) `source_url`) and
> `test_new_routes_require_auth` (auth-gating on the three routes this task added). Also,
> the indirect `__import__("app.db", fromlist=["db"]).get_conn()` is now a plain
> `from app import db`, matching the file's own local-import pattern (`from app import
> seed`, two lines above it).

- [ ] **Step 6: Run — expect failure, then green**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected first: FAIL on `'id="map"'`. Then all passed.

> **Correction (T6 review-fix pass):** with the three tests added above, `tests/` is now
> 41 tests total (was 37). Expect `41 passed`.

- [ ] **Step 7: Look at it**

```bash
.venv/bin/python -m app.seed && ./run.sh
```

Open http://localhost:8788, log in, switch the metro to Miami, search
`retail in Wynwood ~1,500 SF under $8k/mo`.

> **Correction (T6):** this originally said "expect three cards, three pins." Live in the
> browser it is **one** card and **one** pin — Miami's seed data has exactly one `retail`
> listing (`2618 NW 2nd Ave`, 1,500 SF), and `property_type` is a HARD filter (never
> relaxed by the near-miss ladder — see `_LADDER` in `routes_search.py`), so the other two
> Miami seed rows (office, industrial) never qualify regardless of rent/size relaxation.
> The listing's own ask is $95/SF/yr against the query's derived $64/SF/yr cap
> (`$8k/mo * 12 / 1,500 SF`), so the rent-cap stage of the ladder fires. Expect: **one**
> card, **one** pin, the map fitting to Wynwood, a small "Near miss" badge, the reply text
> reading "Nothing matched exactly — I relaxed the rent cap. 1 match. The closest is 2618
> NW 2nd Ave, Miami, FL — ..." (named exactly once — see the T6 review-fix pass correction
> on `ai.reply()` in Task 4, which fixed this same scenario reading the disclosure THREE
> times: the badge, a `routes_search.py` prepend, and `ai.reply()`'s own text), and a click
> opening the listing page. Ctrl-C when done.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(openlease): search UI — metro switcher, HTMX chat search, MapLibre pins, listing page"
```

---

### Task 7: Enrichment providers — geosearch, Overpass, OSRM, bundled rail

All keyless. Each wraps its call in `cache.cached()` so it is fetched once, ever.

**Files:**
- Create: `app/providers/base.py`, `geosearch.py`, `overpass.py`, `osrm.py`
- Create: `app/registry.py`, `app/data/rail/refresh.py`
- Create (generated): `app/data/rail/{nyc,mia,la,chi}.json`
- Modify: `app/db.py` (append `poi` / `transit_nearby` to `SCHEMA`)
- Test: `tests/fixtures/overpass_esb.json` (captured), assertions land in Task 8

**Interfaces:**
- Consumes: `cache.cached` (T1), `models.METROS` (T2).
- Produces:
  - `providers/base.py`: Protocols `ParcelProvider`, `PoiProvider`, `Geocoder`, `Embedder`
  - `overpass.pois(lat: float, lng: float) -> list[dict]` — `[{"category", "lat", "lng", "name", "route_refs"}]`; raises `overpass.OverpassEmpty` on a zero-element response
  - `overpass.OverpassEmpty` (an `Exception`)
  - `osrm.drive_minutes(lat: float, lng: float, metro: str) -> dict[str, float]` — `{"JFK": 31.0, …}`; `osrm.haversine_fallback(...)` same shape
  - `geosearch.geocode(address: str) -> dict | None` — `{lat, lng, bbl, borough, matched}`
  - `rail.stations(metro: str) -> list[dict]` (module `providers/rail.py`, reads the bundled JSON)
  - `registry.poi_provider()`, `registry.geocoder(metro)`, `registry.parcel_provider(metro)` (returns `None` until T9), `registry.embedder()` (returns `None` until T12), `registry.reset()`

- [ ] **Step 1: Append the enrichment tables to `db.SCHEMA`**

```sql
-- Overpass results, cached FOREVER. Buildings do not move, and Overpass 429/504s under
-- request-time load — so this is an INGEST-time fetch, never a search-time one.
CREATE TABLE IF NOT EXISTS poi (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER REFERENCES listing(id) ON DELETE CASCADE,
    category   TEXT NOT NULL,
    name       TEXT,
    lat REAL, lng REAL, meters REAL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_poi_listing ON poi(listing_id);

CREATE TABLE IF NOT EXISTS transit_nearby (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER REFERENCES listing(id) ON DELETE CASCADE,
    mode       TEXT NOT NULL,      -- rail | ferry | bus
    route      TEXT,
    name       TEXT,
    meters     REAL
);
CREATE INDEX IF NOT EXISTS idx_transit_listing ON transit_nearby(listing_id);
```

- [ ] **Step 2: Write `providers/base.py`**

```python
"""Provider interfaces. Each capability is one small Protocol; a concrete provider
implements it and is registered in registry.py. Every provider wraps its network calls
in cache.cached()."""
from typing import Protocol, runtime_checkable

from ..models import Parcel


@runtime_checkable
class Geocoder(Protocol):
    def geocode(self, address: str) -> dict | None:
        """address -> {lat, lng, matched, ...metro-specific join key} or None."""
        ...


@runtime_checkable
class ParcelProvider(Protocol):
    def lookup(self, address: str, lat: float | None, lng: float | None) -> Parcel | None:
        """Address (or point) -> the metro's parcel record, normalized. A field this
        metro does not publish is None WITH a missing_reason — never 0, never a fake."""
        ...


@runtime_checkable
class PoiProvider(Protocol):
    def pois(self, lat: float, lng: float) -> list[dict]:
        """[{category, name, lat, lng, route_refs}] within the Walk Score radius."""
        ...


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """L2-normalized vectors, one per text."""
        ...
```

- [ ] **Step 3: Write `providers/overpass.py`**

```python
"""POIs from OpenStreetMap, at INGEST time only, cached forever.

Two hard-won rules, both spec §2:

1. An EMPTY response is an ERROR, never a score of 0. `overpass.osm.ch` is a
   Switzerland-only extract: it returns HTTP 200 with zero elements for US coordinates,
   which silently scores every American listing 0 and looks like working code. So: only
   the allowlisted mirrors, and a zero-element response raises.
2. Query with `nwr`, not `node` — malls, parks and campuses are ways/relations, and a
   node-only query silently misses them. `out center tags` gives every element a point.
"""
import httpx

from ..cache import cached
from ..config import settings

RADIUS_M = 2414  # Walk Score's outer bound (1.5 miles)

ALLOWED_HOSTS = ("overpass-api.de", "overpass.kumi.systems")

# Walk Score's 9 categories -> OSM tags.
CATEGORIES = {
    "grocery": ('shop', ("supermarket", "grocery", "convenience", "greengrocer")),
    "restaurants": ('amenity', ("restaurant", "fast_food")),
    "shopping": ('shop', ("clothes", "department_store", "mall", "hardware", "electronics")),
    "coffee": ('amenity', ("cafe",)),
    "banks": ('amenity', ("bank",)),
    "parks": ('leisure', ("park", "garden")),
    "schools": ('amenity', ("school",)),
    "books": ('amenity', ("library",)),
    "entertainment": ('amenity', ("cinema", "theatre", "nightclub", "pub", "bar")),
}
_TAG_TO_CATEGORY = {
    (key, val): cat for cat, (key, vals) in CATEGORIES.items() for val in vals
}


class OverpassEmpty(RuntimeError):
    """Zero elements came back. That is a failure, not an empty neighborhood."""


def _query(lat: float, lng: float) -> str:
    parts = []
    for _cat, (key, vals) in CATEGORIES.items():
        parts.append(f'nwr(around:{RADIUS_M},{lat},{lng})[{key}~"^({"|".join(vals)})$"];')
    # bus routes for Transit Score come from the stops' route_ref tag
    parts.append(f'nwr(around:{RADIUS_M},{lat},{lng})[highway=bus_stop];')
    # 120s, not 60: this query genuinely needs ~43s of Overpass compute over a dense metro
    # and 504'd twice at [timeout:60]. See the correction note below. INGEST-TIME only.
    return f"[out:json][timeout:120];\n({chr(10).join(parts)}\n);\nout center tags;"


def pois(lat: float, lng: float) -> list[dict]:
    """One call, every category. Cached forever (cost 0 — Overpass is free)."""
    host = httpx.URL(settings.overpass_url).host
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(
            f"{host} is not an allowlisted Overpass mirror {ALLOWED_HOSTS}. "
            "overpass.osm.ch in particular returns 200 + zero elements for US coords."
        )
    q = _query(lat, lng)

    def fetch():
        r = httpx.post(settings.overpass_url, data={"data": q}, timeout=90.0)
        r.raise_for_status()
        return r.json()

    data = cached("overpass", "interpreter", {"lat": round(lat, 5), "lng": round(lng, 5)}, fetch)
    els = data.get("elements", [])
    if not els:
        raise OverpassEmpty(
            f"Overpass returned zero elements for {lat},{lng}. Treating this as a FAILURE — "
            "a real address always has something within 1.5 miles. Check the mirror."
        )
    return [_normalize(e) for e in els if _normalize(e)]


def _normalize(e: dict) -> dict | None:
    tags = e.get("tags") or {}
    center = e.get("center") or {}
    lat, lng = e.get("lat", center.get("lat")), e.get("lon", center.get("lon"))
    if lat is None or lng is None:
        return None
    if tags.get("highway") == "bus_stop":
        return {"category": "bus_stop", "name": tags.get("name"), "lat": lat, "lng": lng,
                "route_refs": [r.strip() for r in (tags.get("route_ref") or "").split(";") if r.strip()]}
    for (key, val), cat in _TAG_TO_CATEGORY.items():
        if tags.get(key) == val:
            return {"category": cat, "name": tags.get("name"), "lat": lat, "lng": lng,
                    "route_refs": []}
    return None
```

> **Correction (Task 7, verified live 2026-07-11/12):** two real defects found hitting
> `overpass-api.de` for the fixture captures below:
>
> 1. **`overpass-api.de` returns HTTP 406 to httpx's default `User-Agent`** header
>    (`python-httpx/x.y.z`). Confirmed with a clean A/B on the same query, same endpoint,
>    back-to-back: default UA → 406 (×3); `curl/8.0` or an identifying bot UA → 200. Every
>    call in `fetch()` now sends `headers={"User-Agent": settings.crawl_user_agent}`.
> 2. **`[timeout:60]` is marginal for the full 9-category + bus-stop query over a dense
>    downtown point.** The Empire State Building fixture capture 504'd twice at
>    `[timeout:60]`/`timeout=90.0` and succeeded at `[timeout:120]`/`timeout=150.0`, taking
>    ~43s of real server-side compute once it did. Since this call is INGEST-TIME ONLY and
>    cached forever, there is no cost to more headroom — but a false "mirror is down" on
>    exactly the densest, highest-POI-count addresses is a real cost. `_query()` now emits
>    `[out:json][timeout:120]`, and `fetch()`'s httpx timeout is `150.0`.
>
> Both fixes are in `app/providers/overpass.py`; see `task-7-report.md` for the raw
> before/after HTTP evidence.

- [ ] **Step 4: Write `providers/osrm.py`**

```python
"""Airport drive times. ONE keyless OSRM /table call returns every airport in the metro.

OSRM's public router is FREE-FLOW — no traffic. Its Midtown->JFK is 31 minutes against a
real 45-60. That is not a bug to fix, it is a number to LABEL: the UI says "no traffic".
Offline, we fall back to a power law fitted to OSRM's own answers, which underestimates
any route crossing a bridge or water."""
import math

import httpx

from ..cache import cached
from ..config import settings
from ..models import METROS


def haversine_mi(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def haversine_fallback(lat: float, lng: float, metro: str) -> dict[str, float]:
    """drive_min = 5.31 * miles^0.718 — fitted to OSRM. Underestimates water crossings."""
    out = {}
    for code, (alat, alng) in METROS[metro]["airports"].items():
        mi = haversine_mi(lat, lng, alat, alng)
        out[code] = round(5.31 * (mi ** 0.718), 1)
    return out


def drive_minutes(lat: float, lng: float, metro: str) -> dict[str, float]:
    airports = METROS[metro]["airports"]
    coords = ";".join([f"{lng},{lat}"] + [f"{a[1]},{a[0]}" for a in airports.values()])
    url = f"{settings.osrm_url}/table/v1/driving/{coords}?sources=0&annotations=duration"

    def fetch():
        r = httpx.get(url, timeout=30.0)
        r.raise_for_status()
        return r.json()

    try:
        data = cached("osrm", "table", {"lat": round(lat, 5), "lng": round(lng, 5), "metro": metro}, fetch)
        durations = data["durations"][0][1:]          # [0] is the origin to itself
        return {code: round(d / 60.0, 1) for code, d in zip(airports, durations) if d is not None}
    except Exception:  # noqa: BLE001 — offline / rate-limited: the power law still answers
        return haversine_fallback(lat, lng, metro)
```

> **Correction (Task 7):** two small additions, neither changing the documented behavior:
> `fetch()` now sends `headers={"User-Agent": settings.crawl_user_agent}` (OSRM's public
> router didn't require this live, unlike Overpass — but it costs nothing and matches the
> rest of the app's outbound-request etiquette). And the bare `except Exception: return
> haversine_fallback(...)` now logs a WARNING naming the exception before falling back —
> matching this codebase's one hard rule about fallbacks ("a silent fallback hid a 400 for
> OpenProp's entire life", constraints.md) instead of swallowing it silently.

- [ ] **Step 5: Write `providers/geosearch.py` and `providers/rail.py`**

`geosearch.py` — NYC's free, keyless geocoder; it is also how we get a BBL, which is the
parcel join key:

```python
"""NYC GeoSearch (geosearch.planninglabs.nyc) — free, keyless, and it hands back the BBL
in `addendum.pad.bbl`, which is exactly the PLUTO join key. The other three metros use
their own ArcGIS/Socrata address search (see each parcel provider)."""
import httpx

from ..cache import cached

URL = "https://geosearch.planninglabs.nyc/v2/search"


def geocode(address: str) -> dict | None:
    def fetch():
        r = httpx.get(URL, params={"text": address, "size": 1}, timeout=20.0)
        r.raise_for_status()
        return r.json()

    data = cached("geosearch", "search", {"text": address}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None
    f = feats[0]
    lng, lat = f["geometry"]["coordinates"]
    props = f.get("properties", {})
    bbl = (props.get("addendum", {}).get("pad", {}) or {}).get("bbl")
    return {"lat": lat, "lng": lng, "bbl": str(bbl) if bbl else None,
            "borough": props.get("borough"), "matched": props.get("label")}
```

> **Correction (Task 7, verified live):** `fetch()` sends the same
> `headers={"User-Agent": settings.crawl_user_agent}` as the other two providers. GeoSearch
> answered fine with httpx's default UA in manual testing (real response captured for "350
> 5th Ave, New York, NY" → `bbl: "1008350041"`, `borough: "Manhattan"`), so this isn't fixing
> an observed failure — it's the same low-cost consistency change as `osrm.py`.

`rail.py` — the bundled stations. **No API, no failure mode**:

```python
"""Rail/ferry stations as BUNDLED STATIC JSON — ~800 points across four metros, <100KB.
Zero API calls at runtime and therefore zero runtime failure modes. Regenerate with
`python -m app.data.rail.refresh` when an agency opens a station."""
import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent.parent / "data" / "rail"


@lru_cache
def stations(metro: str) -> list[dict]:
    """[{name, lat, lng, mode, routes: []}] — mode is 'rail' or 'ferry'."""
    p = _DIR / f"{metro}.json"
    return json.loads(p.read_text()) if p.exists() else []
```

- [ ] **Step 6: Write `data/rail/refresh.py` and generate the four bundles**

```python
"""Build-time only: regenerate the bundled rail-station JSON from each agency's open data.
Not imported by the app — the app reads the JSON. Run when an agency opens a station:

    python -m app.data.rail.refresh

Sources (all keyless, verified 2026-07-11, spec §7):
  nyc — data.ny.gov 39hk-dx4f (subway entrances/stations, 496)
  mia — Miami-Dade ArcGIS MetroRailStations_gdb (23) + Metromover (21)
  la  — LA Metro GTFS gitlab.com/LACMTA/gtfs_rail -> stops.txt where location_type=1 (111)
  chi — data.cityofchicago.org 3tzw-cg4m (CTA 'L' stops, 145)
"""
import csv
import io
import json
import zipfile
from pathlib import Path

import httpx

OUT = Path(__file__).parent


def _write(metro: str, rows: list[dict]) -> None:
    (OUT / f"{metro}.json").write_text(json.dumps(rows, indent=0))
    print(f"{metro}: {len(rows)} stations")


def nyc() -> None:
    r = httpx.get("https://data.ny.gov/resource/39hk-dx4f.json",
                  params={"$limit": 2000}, timeout=60.0)
    r.raise_for_status()
    seen, rows = set(), []
    for s in r.json():
        name = s.get("stop_name") or s.get("station_name")
        lat, lng = s.get("gtfs_latitude") or s.get("latitude"), s.get("gtfs_longitude") or s.get("longitude")
        if not (name and lat and lng) or name in seen:
            continue
        seen.add(name)
        rows.append({"name": name, "lat": float(lat), "lng": float(lng), "mode": "rail",
                     "routes": (s.get("daytime_routes") or "").split()})
    _write("nyc", rows)


def mia() -> None:
    rows = []
    for url, mode in [
        ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/MetroRailStations_gdb/FeatureServer/0/query", "rail"),
        ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/MetroMoverStations_gdb/FeatureServer/0/query", "rail"),
    ]:
        r = httpx.get(url, params={"where": "1=1", "outFields": "*", "f": "geojson"}, timeout=60.0)
        r.raise_for_status()
        for f in r.json().get("features", []):
            lng, lat = f["geometry"]["coordinates"]
            p = f["properties"]
            rows.append({"name": p.get("NAME") or p.get("STATION"), "lat": lat, "lng": lng,
                         "mode": mode, "routes": []})
    _write("mia", rows)


def la() -> None:
    r = httpx.get("https://gitlab.com/LACMTA/gtfs_rail/-/raw/master/gtfs_rail.zip",
                  follow_redirects=True, timeout=120.0)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        stops = list(csv.DictReader(io.TextIOWrapper(z.open("stops.txt"), "utf-8-sig")))
    rows = [{"name": s["stop_name"], "lat": float(s["stop_lat"]), "lng": float(s["stop_lon"]),
             "mode": "rail", "routes": []}
            for s in stops if s.get("location_type") == "1"]
    _write("la", rows)


def chi() -> None:
    r = httpx.get("https://data.cityofchicago.org/resource/3tzw-cg4m.json",
                  params={"$limit": 1000}, timeout=60.0)
    r.raise_for_status()
    seen, rows = set(), []
    for s in r.json():
        name = s.get("station_name")
        loc = s.get("location") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if not (name and lat and lng) or name in seen:
            continue
        seen.add(name)
        routes = [k.upper() for k in ("red", "blue", "g", "brn", "p", "y", "pnk", "o")
                  if str(s.get(k)).lower() == "true"]
        rows.append({"name": name, "lat": float(lat), "lng": float(lng), "mode": "rail",
                     "routes": routes})
    _write("chi", rows)


if __name__ == "__main__":
    for fn in (nyc, mia, la, chi):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — one agency being down must not block the rest
            print(f"{fn.__name__} FAILED: {type(e).__name__}: {e}")
```

> **Correction (Task 7, verified live 2026-07-11/12):** two real bugs found running this
> against the actual endpoints, both silent (neither raises — they just produce the wrong
> row count):
>
> 1. **`nyc()`'s dedup key was wrong.** `data.ny.gov/resource/39hk-dx4f.json` returns 496
>    rows with a UNIQUE `gtfs_stop_id` per row, but `stop_name` is NOT unique — 76 names
>    repeat across distinct physical stops on different lines/divisions (e.g. "Canal St" is
>    6 separate stops, "Times Sq-42 St" is 4). Deduping on `name in seen` as originally
>    written collapsed 496 rows to **379**, silently discarding 117 real stations. Fixed:
>    dedup on `gtfs_stop_id` instead (confirmed 496 unique of 496 rows).
> 2. **`chi()`'s field names don't exist in the live dataset.** `3tzw-cg4m`'s schema has
>    changed since the plan was drafted: there is no `station_name` column, no `location`
>    object, and no per-line boolean columns (`red`, `blue`, `g`, …). Every row's `name` was
>    `None`, so `if not (name and lat and lng): continue` skipped **all 145 rows**, writing
>    an EMPTY `chi.json` every time — an ingest-time silent-zero, exactly the failure mode
>    this whole task is designed to avoid, just one layer up (bad upstream schema instead of
>    an empty Overpass response). The real columns are `longname` (station name), `the_geom`
>    (a GeoJSON Point, `coordinates: [lng, lat]`), and a free-text `lines` field like
>    `"Brown, Orange, Pink, Purple (Express), Green"`. Fixed: read `longname`/`the_geom`,
>    dedup on the real `station_id` (145 unique of 145 rows), and recover route colors by
>    searching `lines` for the 8 CTA line-color names (`Red`, `Blue`, `Brown`, `Green`,
>    `Orange`, `Pink`, `Purple`, `Yellow`) instead of reading nonexistent boolean columns.
>
> Both were caught by actually running the script against the live endpoints (per this
> task's Step 6 instructions) rather than trusting the plan's code — see `task-7-report.md`
> for the raw request/response evidence. `app/data/rail/refresh.py` has the corrected code;
> the real generated bundles (committed) come out to NYC 496 / Miami 44 / LA 111 / Chicago
> 145 — matching the spec exactly.

Generate the bundles (this is a build step, run once; the JSON is committed):

```bash
.venv/bin/python -m app.data.rail.refresh
wc -c app/data/rail/*.json
```

Expected: four files, ~800 stations total, well under 100KB combined. If an agency URL has
moved, fix it here — the app never calls these at runtime.

- [ ] **Step 7: Write `registry.py`**

```python
"""Capability -> active provider. Lazy: a provider only imports when first requested, so
the app boots even before a key is configured. Returns None when a capability has no
usable provider — callers degrade gracefully."""
from functools import lru_cache

from .config import settings


def reset() -> None:
    """Drop every cached provider — called after settings change so the next access
    rebuilds with the current keys."""
    for fn in (geocoder, parcel_provider, poi_provider, embedder):
        fn.cache_clear()


@lru_cache
def geocoder(metro: str):
    if metro == "nyc":
        from .providers import geosearch
        return geosearch
    # the other three geocode through their own parcel provider's address search
    return parcel_provider(metro)


@lru_cache
def parcel_provider(metro: str):
    mod = {"nyc": "parcel_nyc", "mia": "parcel_miami",
           "la": "parcel_la", "chi": "parcel_chicago"}.get(metro)
    if not mod:
        return None
    try:
        import importlib
        return importlib.import_module(f".providers.{mod}", __package__)
    except ModuleNotFoundError:      # not built yet (T9)
        return None


@lru_cache
def poi_provider():
    from .providers import overpass
    return overpass          # free, no key


@lru_cache
def embedder():
    if not settings.voyage_api_key:
        return None          # BM25-only; RRF over one list is order-preserving
    from .providers.voyage import VoyageEmbedder
    return VoyageEmbedder()
```

> **Correction (Task 7):** `parcel_provider`'s bare `except ModuleNotFoundError: return
> None` has the exact same latent bug `settings_store.save()`'s guard was written to avoid
> (see that file's Step 5/review correction above): it would swallow a ModuleNotFoundError
> raised from *inside* an already-built `parcel_nyc.py` (e.g. a typo'd import in Task 9) and
> silently reinterpret a real bug as "not built yet." Fixed with the same `exc.name` check:
>
> ```python
> @lru_cache
> def parcel_provider(metro: str):
>     mod = {"nyc": "parcel_nyc", "mia": "parcel_miami",
>            "la": "parcel_la", "chi": "parcel_chicago"}.get(metro)
>     if not mod:
>         return None
>     import importlib
>     full_name = f"{__package__}.providers.{mod}"
>     try:
>         return importlib.import_module(full_name)
>     except ModuleNotFoundError as exc:
>         if exc.name != full_name:
>             raise   # a real bug inside an already-built module — don't hide it
>         return None  # THIS module doesn't exist yet (not built until T9)
> ```
>
> Covered by `tests/test_registry.py::test_parcel_provider_does_not_swallow_a_real_bug_inside_a_built_module`.

- [ ] **Step 8: Capture the Overpass fixture that Task 8 tests against**

The Walk Score anchors (Empire State Building = 100, Bay Ridge = 98) are the spec's
validation. Capture the real POI sets once, commit them, and the score test becomes fast,
offline, and deterministic.

```bash
.venv/bin/python -c "
import json
from app.providers import overpass
for name, lat, lng in [('esb', 40.7484, -73.9857), ('bay_ridge', 40.6280, -74.0300),
                       ('vernon_la', 34.0033, -118.2100)]:
    pois = overpass.pois(lat, lng)
    json.dump({'lat': lat, 'lng': lng, 'pois': pois},
              open(f'tests/fixtures/overpass_{name}.json', 'w'))
    print(name, len(pois), 'pois')
"
```

Expected: `esb` in the low thousands, `bay_ridge` in the hundreds, `vernon_la` far fewer
(an industrial district — the low-score control). If any of these returns zero,
`OverpassEmpty` fires; that means the mirror is wrong, not the neighborhood.

- [ ] **Step 9: Prove the allowlist actually bites**

```bash
.venv/bin/python -c "
from app.config import settings
from app.providers import overpass
settings.overpass_url = 'https://overpass.osm.ch/api/interpreter'
try:
    overpass.pois(40.7484, -73.9857); print('FAIL — the Swiss mirror was allowed')
except RuntimeError as e:
    print('OK —', e)"
```

Expected: `OK — overpass.osm.ch is not an allowlisted Overpass mirror …`

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(openlease): keyless enrichment — Overpass (allowlisted, empty=error), OSRM, GeoSearch, bundled rail"
```

---

### Task 8: `score.py` — Walk Score and Transit Score, the published methodology

Not a hand-rolled heuristic: the actual 2011 Walk Score algorithm, whose published anchors
we can check ourselves.

**Files:**
- Create: `app/score.py`
- Modify: `app/templates/listing.html` (score breakdown block)
- Test: `tests/test_score.py`

**Interfaces:**
- Consumes: `overpass.pois`, `rail.stations` (T7).
- Produces:
  - `score.decay(meters: float) -> float`
  - `score.walk_score(lat: float, lng: float, pois: list[dict]) -> tuple[int, dict]` — `(0..100, breakdown)`; breakdown is `{category: {"score": float, "weight": float, "nearest_m": float, "count": int}}`
  - `score.transit_score(lat: float, lng: float, pois: list[dict], stations: list[dict]) -> tuple[int, list[dict]]` — `(0..100, nearby)`
  - `score.enrich(listing_id: int) -> dict` — fetches, scores, persists `walk_score` / `transit_score` / `score_breakdown_json` / `poi` / `transit_nearby`

- [ ] **Step 1: Write `score.py`**

```python
"""Walk Score and Transit Score — Walk Score's own PUBLISHED methodology, not a heuristic.

Walk Score (2011): 9 categories, weights summing to 15, multiplied by 6.67 -> 0-100.
Distance decay: amenities within 402m (.25mi) score full credit; credit falls to zero at
2414m (1.5mi). The published curve is

    decay(d) = ((2414 - d) / 2012) ^ 2.3135      clamped to 1.0 below 402m, 0 above 2414m

which is solved from Walk Score's three published anchors. Validated against their own
published values: Empire State Building = 100, Bay Ridge = 98.

Transit Score is aggregated PER ROUTE, not per stop — twelve buses on one line is one
route, and counting stops would triple-score a bus corridor:

    raw = Σ_routes (trips_per_week × mode_weight × decay(nearest stop on that route))

log-normalized to 0-100. Mode weights: rail 2, ferry 1.5, bus 1.
"""
import json
import math

from .db import get_conn, get_listing
from .providers import overpass, rail

FULL_CREDIT_M = 402.0
ZERO_CREDIT_M = 2414.0
_EXP = 2.3135
_SPAN = 2012.0

# category -> the weight of the Nth-nearest amenity in it. Walk Score gives depth to the
# categories where variety matters (you want ten restaurants, not ten banks).
WEIGHTS: dict[str, list[float]] = {
    "grocery":       [3.0],
    "restaurants":   [0.75, 0.45, 0.25, 0.25, 0.225, 0.225, 0.225, 0.225, 0.2, 0.2],
    "shopping":      [0.5, 0.45, 0.4, 0.35, 0.3],
    "coffee":        [1.25, 0.75],
    "banks":         [1.0],
    "parks":         [1.0],
    "schools":       [1.0],
    "books":         [1.0],
    "entertainment": [1.0],
}
MAX_WEIGHT = sum(sum(w) for w in WEIGHTS.values())   # 15.0 — Walk Score's own "sums to 15"
MULTIPLIER = 6.67

MODE_WEIGHT = {"rail": 2.0, "ferry": 1.5, "bus": 1.0}
# ponytail: trips/week without a GTFS feed is a per-mode constant. It is the one number
# here that is NOT published; it moves the normalization, not the ordering. Upgrade path:
# read trips/week from each agency's GTFS if the rankings ever look wrong.
TRIPS_PER_WEEK = {"rail": 700.0, "ferry": 200.0, "bus": 350.0}
# ponytail: calibration constant. The spec flags this as needing a fit against ~20 known
# addresses; 4000 is an EYEBALLED guess (not fit against verified ground truth) that puts
# Midtown near 100 and a Vernon industrial block near 30. No `--calibrate` tooling exists —
# do not quote Transit Score as gospel until someone builds that fit; until then the UI
# label must say "a ranking, not a rating."
TRANSIT_NORM = 4000.0


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def decay(meters: float) -> float:
    """1.0 within 402m, 0.0 beyond 2414m, Walk Score's published curve between."""
    if meters <= FULL_CREDIT_M:
        return 1.0
    if meters >= ZERO_CREDIT_M:
        return 0.0
    return ((ZERO_CREDIT_M - meters) / _SPAN) ** _EXP


def walk_score(lat: float, lng: float, pois: list[dict]) -> tuple[int, dict]:
    """(0-100, per-category breakdown). The breakdown is a UI element, not debug output:
    it explains the score instead of asserting it."""
    breakdown: dict[str, dict] = {}
    total = 0.0
    for cat, weights in WEIGHTS.items():
        dists = sorted(
            haversine_m(lat, lng, p["lat"], p["lng"])
            for p in pois if p.get("category") == cat
        )
        earned = sum(w * decay(d) for w, d in zip(weights, dists))
        total += earned
        breakdown[cat] = {
            "score": round(earned, 3),
            "weight": round(sum(weights), 3),
            "count": len(dists),
            "nearest_m": round(dists[0]) if dists else None,
        }
    return min(100, round(total * MULTIPLIER)), breakdown


def transit_score(lat: float, lng: float, pois: list[dict],
                  stations: list[dict]) -> tuple[int, list[dict]]:
    """Per ROUTE, not per stop. Bus routes come from the OSM stops' `route_ref` tag;
    rail/ferry from the bundled station JSON."""
    best: dict[tuple[str, str], float] = {}   # (mode, route) -> nearest meters
    nearby: list[dict] = []

    for s in stations:
        d = haversine_m(lat, lng, s["lat"], s["lng"])
        if d > ZERO_CREDIT_M:
            continue
        mode = s.get("mode", "rail")
        routes = s.get("routes") or [s["name"]]   # an unrouted station is its own "route"
        for rt in routes:
            key = (mode, rt)
            if d < best.get(key, math.inf):
                best[key] = d
        nearby.append({"mode": mode, "route": ",".join(routes), "name": s["name"],
                       "meters": round(d)})

    for p in pois:
        if p.get("category") != "bus_stop":
            continue
        d = haversine_m(lat, lng, p["lat"], p["lng"])
        if d > ZERO_CREDIT_M:
            continue
        for rt in (p.get("route_refs") or []):
            key = ("bus", rt)
            if d < best.get(key, math.inf):
                best[key] = d

    raw = sum(
        TRIPS_PER_WEEK[mode] * MODE_WEIGHT[mode] * decay(d)
        for (mode, _rt), d in best.items()
    )
    scaled = 100.0 * math.log1p(raw) / math.log1p(TRANSIT_NORM)
    nearby.sort(key=lambda n: n["meters"])
    return min(100, round(scaled)), nearby[:8]


def enrich(listing_id: int) -> dict:
    """Fetch POIs once, score, persist. Raises OverpassEmpty rather than storing a 0 —
    a listing with no score is honest; a listing scored 0 because the mirror was wrong
    is a lie the UI can't detect."""
    row = get_listing(listing_id)
    if not row or row.get("lat") is None:
        return {}
    lat, lng = row["lat"], row["lng"]
    ps = overpass.pois(lat, lng)                       # raises OverpassEmpty on failure
    ws, breakdown = walk_score(lat, lng, ps)
    ts, nearby = transit_score(lat, lng, ps, rail.stations(row["metro"]))

    with get_conn() as conn:
        conn.execute("DELETE FROM poi WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO poi (listing_id, category, name, lat, lng, meters) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(listing_id, p["category"], p.get("name"), p["lat"], p["lng"],
              round(haversine_m(lat, lng, p["lat"], p["lng"])))
             for p in ps if p["category"] != "bus_stop"],
        )
        conn.execute("DELETE FROM transit_nearby WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO transit_nearby (listing_id, mode, route, name, meters) "
            "VALUES (?, ?, ?, ?, ?)",
            [(listing_id, n["mode"], n["route"], n["name"], n["meters"]) for n in nearby],
        )
        conn.execute(
            "UPDATE listing SET walk_score = ?, transit_score = ?, score_breakdown_json = ? "
            "WHERE id = ?",
            (ws, ts, json.dumps(breakdown), listing_id),
        )
    return {"walk_score": ws, "transit_score": ts, "breakdown": breakdown}


def demo() -> None:
    assert decay(0) == 1.0 and decay(402) == 1.0
    assert decay(2414) == 0.0 and decay(3000) == 0.0
    assert 0.0 < decay(1200) < 1.0
    assert decay(500) > decay(1500) > decay(2400)
    assert abs(MAX_WEIGHT - 15.0) < 0.05, MAX_WEIGHT     # "weights sum to 15"
    assert round(MAX_WEIGHT * MULTIPLIER) == 100, MAX_WEIGHT * MULTIPLIER
    print("score.demo (decay curve + weight normalization) OK")


if __name__ == "__main__":
    demo()
```

> **Correction (Task 8, verified live 2026-07-12):** three defects found implementing and
> running this against the real seed listings (all fixed in `app/score.py` /
> `templates/listing.html`, none change the published methodology or the test assertions
> above):
>
> 1. **`MAX_WEIGHT`'s inline comment was wrong.** The WEIGHTS table above sums to exactly
>    `15.0` (verified with `Decimal`, not just float tolerance) — not `14.975`. The
>    `abs(MAX_WEIGHT - 15.0) < 0.05` test tolerance masked the stale comment. Corrected to
>    say `15.0`.
> 2. **The TRANSIT_NORM comment promised a `python -m app.score --calibrate` flag that was
>    never implemented** (`__main__` only ever calls `demo()`). No dataset of ~20
>    known-Transit-Score addresses exists to fit against, so no calibration was attempted —
>    `TRANSIT_NORM = 4000.0` and `TRIPS_PER_WEEK` remain the eyeballed guesses the spec
>    already flagged them as. The comment now says that plainly instead of pointing at a
>    command that doesn't exist.
> 3. **Step 4's listing-page copy claimed the Transit Score normalization "is calibrated,
>    not published"** — asserting a calibration that was never done, which contradicts this
>    same plan's own § Layer 2 note ("Normalization constant needs calibration against ~20
>    known addresses") and the constraint against silently pretending an uncalibrated
>    number is calibrated. Reworded to "is an uncalibrated estimate, not a published value."
>
> Separately (not a code defect, an operational observation): running `score.enrich()` back
> to back for all 12 seed listings with no delay hit `overpass-api.de`'s rate limiting —
> several calls came back `406`/`429` even with the correct `crawl_user_agent` header from
> the Task 7 fix. Each failure surfaced as a loud `HTTPStatusError`, never a silent 0, so the
> "empty response is an error" contract held — but a real bulk backfill needs a delay
> between successive Overpass calls, which belongs in a future bulk-ingest task, not here.
> With an 8s gap between listings, all 12 succeeded; see `task-8-report.md` for the full set
> of real scores.
>
> One more doc-only fix: the **Interfaces** line above originally listed `osrm.drive_minutes`
> as something `score.py` consumes. It never imports or calls it — airport drive times are
> an unrelated feature (T7's `osrm.py`), not an input to Walk/Transit Score. Removed.

- [ ] **Step 2: Write the failing test**

`tests/test_score.py`:

```python
"""Walk Score against Walk Score's OWN published values (ESB=100, Bay Ridge=98), off the
committed Overpass fixtures — fast, offline, deterministic. The decay curve is checked
against its three published anchors directly."""
import json
import os
import pathlib
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_score.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402

from app import score  # noqa: E402
from app.providers import rail  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name: str) -> dict:
    p = FIX / f"overpass_{name}.json"
    if not p.exists():
        pytest.skip(f"{p.name} not captured — see Task 7 Step 8")
    return json.loads(p.read_text())


def test_decay_matches_published_anchors():
    assert score.decay(0) == 1.0
    assert score.decay(402) == 1.0          # full credit inside a quarter mile
    assert score.decay(2414) == 0.0         # zero credit at 1.5 miles
    assert score.decay(3000) == 0.0
    assert score.decay(500) > score.decay(1500) > score.decay(2400) > 0.0


def test_weights_sum_to_fifteen_and_normalize_to_100():
    assert abs(score.MAX_WEIGHT - 15.0) < 0.05, score.MAX_WEIGHT
    assert round(score.MAX_WEIGHT * score.MULTIPLIER) == 100


def test_empire_state_building_scores_100():
    f = _fx("esb")
    s, breakdown = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert s >= 98, (s, {k: v["score"] for k, v in breakdown.items()})
    assert breakdown["grocery"]["nearest_m"] is not None
    assert breakdown["restaurants"]["count"] > 20


def test_bay_ridge_scores_high_but_below_midtown():
    f = _fx("bay_ridge")
    s, _ = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert 93 <= s <= 100, s      # Walk Score publishes 98
    esb = _fx("esb")
    assert s <= score.walk_score(esb["lat"], esb["lng"], esb["pois"])[0]


def test_industrial_district_scores_low():
    """The control: if a Vernon industrial block also scored ~100, the score would be
    measuring nothing."""
    f = _fx("vernon_la")
    s, _ = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert s < 75, s


def test_empty_pois_is_never_a_zero_score():
    """An empty Overpass response is an ERROR upstream (overpass.OverpassEmpty). It must
    never arrive here as an honest 0 — but if a caller ever passes [], the score is 0 AND
    every category reads count=0, which the UI can see. The guard that matters lives in
    overpass.pois(); this pins the contract."""
    s, breakdown = score.walk_score(40.7484, -73.9857, [])
    assert s == 0
    assert all(v["count"] == 0 and v["nearest_m"] is None for v in breakdown.values())


def test_transit_score_counts_routes_not_stops():
    # ten stops on ONE bus route must not outscore one stop on a rail line
    bus = [{"category": "bus_stop", "lat": 40.7484 + i * 0.0001, "lng": -73.9857,
            "route_refs": ["M4"]} for i in range(10)]
    one_bus, _ = score.transit_score(40.7484, -73.9857, bus, [])
    rail_one, _ = score.transit_score(40.7484, -73.9857, [], [
        {"name": "34 St-Herald Sq", "lat": 40.7497, "lng": -73.9877, "mode": "rail",
         "routes": ["B", "D", "F", "M", "N", "Q", "R", "W"]}])
    assert rail_one > one_bus, (rail_one, one_bus)


# NOTE (review pass): `rail_one > one_bus` CANNOT catch a per-stop regression -- the
# log-normalization pins both near the 100 ceiling, so summing the ten M4 stops
# individually still leaves the 8-route rail hub ahead and the assertion still passes.
# The invariant with teeth is that a route is worth the same however many stops the
# agency happened to map it with:
def test_one_route_scores_the_same_however_many_stops_it_has():
    at = dict(lat=40.7484, lng=-73.9857)
    one = [{"category": "bus_stop", "lat": 40.7520, "lng": -73.9857, "route_refs": ["M4"]}]
    ten = [dict(one[0]) for _ in range(10)]          # same route, same distance, ten times
    s_one, _ = score.transit_score(at["lat"], at["lng"], one, [])
    s_ten, _ = score.transit_score(at["lat"], at["lng"], ten, [])
    assert s_one == s_ten, (s_one, s_ten)            # per-stop summing makes ten ~10x the one
    assert s_one > 0, "the single M4 stop must score something, or this proves nothing"
    two_routes = [dict(one[0], route_refs=["M4"]), dict(one[0], route_refs=["M104"])]
    s_two, _ = score.transit_score(at["lat"], at["lng"], two_routes, [])
    assert s_two > s_one, (s_two, s_one)             # a genuinely second route DOES count
    assert 0 <= one_bus <= 100 and 0 <= rail_one <= 100


def test_bundled_rail_is_present_for_every_metro():
    for metro, floor in [("nyc", 400), ("mia", 30), ("la", 90), ("chi", 100)]:
        st = rail.stations(metro)
        assert len(st) >= floor, (metro, len(st))
        assert all({"name", "lat", "lng", "mode"} <= set(s) for s in st)
```

- [ ] **Step 3: Run — expect failure, then green**

```bash
.venv/bin/python -m app.score && .venv/bin/python -m pytest tests/test_score.py -v
```

Expected: `score.demo (decay curve + weight normalization) OK`, then `8 passed`.
If ESB comes in under 98, print the breakdown — a category scoring 0 means its OSM tag
mapping in `overpass.CATEGORIES` is wrong, not that Midtown lacks coffee.

- [ ] **Step 4: Add the breakdown to the listing page**

In `templates/listing.html`, replace `{% block listing_extra %}{% endblock %}` with:

```html
{% if l.scoreBreakdown %}
<div class="rounded-lg border bg-white p-4">
  <h2 class="text-sm font-semibold mb-2">
    Walk Score {{ l.walkScore }}<span class="text-slate-400 font-normal"> / 100</span>
    {% if l.transitScore is not none %}   <!-- 0 is a REAL score (transit desert), not 'uncomputed' -->
    <span class="ml-3">Transit Score {{ l.transitScore }}<span class="text-slate-400 font-normal"> / 100</span></span>
    {% endif %}
  </h2>
  <p class="text-xs text-slate-400 mb-3">
    Walk Score's published 2011 methodology, computed here from OpenStreetMap. Transit
    Score's normalization constant is an uncalibrated estimate, not a published value —
    treat it as a ranking, not a rating.
  </p>
  <div class="space-y-1">
    {% for cat, b in l.scoreBreakdown.items() %}
    <div class="flex items-center gap-2 text-xs">
      <span class="w-24 text-slate-500">{{ cat }}</span>
      <div class="flex-1 h-2 rounded bg-slate-100">
        <div class="h-2 rounded bg-sky-500"
             style="width: {{ (100 * b.score / b.weight) | round | int if b.weight else 0 }}%"></div>
      </div>
      <span class="w-28 text-right text-slate-400">
        {% if b.nearest_m %}{{ b.count }} · nearest {{ b.nearest_m }}m{% else %}none within 1.5mi{% endif %}
      </span>
    </div>
    {% endfor %}
  </div>
</div>
{% endif %}
```

- [ ] **Step 5: Score the seed data and look at it**

```bash
.venv/bin/python -c "
from app import db, score, seed
seed.seed()
with db.get_conn() as c:
    ids = [r['id'] for r in c.execute('SELECT id FROM listing').fetchall()]
for i in ids:
    try:
        r = score.enrich(i)
        print(i, r.get('walk_score'), r.get('transit_score'))
    except Exception as e:
        print(i, 'FAILED', type(e).__name__, e)
"
```

Expected: Wynwood and Meatpacking in the 90s; Vernon and Archer Heights far lower; no zeros
from an empty response (that would raise instead). Then `./run.sh`, open a listing, and
confirm the breakdown bars render.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(openlease): Walk/Transit Score — published methodology, validated ESB=100 / Bay Ridge=98"
```

---

### Task 9: Parcel providers — one per metro, `null` with a reason

Four ArcGIS/Socrata APIs, four join keys, four different holes in the data. The holes are
the feature: a field a metro does not publish comes back `None` **with a reason string**,
which the listing page renders as "not published here" — never a blank, never a 0.

**Files:**
- Create: `app/providers/parcel_nyc.py`, `parcel_miami.py`, `parcel_la.py`, `parcel_chicago.py`
- Modify: `app/db.py` (parcel persistence), `app/routes_listings.py` (fill the `parcel` context)
- Test: `tests/test_parcel.py` + `tests/fixtures/parcel_*.json`

**Interfaces:**
- Consumes: `cache.cached` (T1), `models.Parcel`, `models.METROS` (T2), `providers/base.ParcelProvider` (T7).
- Produces (each module):
  - `lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None`
  - `normalize(raw: dict) -> Parcel` — pure, no network; this is what the tests hit
  - `db.save_parcel(p: Parcel) -> str` (returns `parcel_id`), `db.get_parcel(parcel_id: str) -> dict | None`

> **Reality check (landed 2026-07-12) — every one of the four endpoints below had drifted
> from what this plan assumed.** Same lesson as Task 7's rail schema: an endpoint that's
> "verified live" on the day the spec was written can still be pointing at the wrong
> resource, or the wrong field name, or a resource that's been quietly split in two. The
> code blocks below are corrected to what's ACTUALLY live; nothing here was theoretical —
> every fix was found by running the real capture script in Step 6 and staring at a
> `NO MATCH` or an empty attribute where a value should have been:
> - **NYC (`64uk-42ks`)**: field names held (`ownername`, `zonedist1`, `builtfar`, ...). The
>   one surprise: PLUTO's `bbl` column comes back as `"1008350041.00000000"`, not the clean
>   10-digit string GeoSearch hands us — used verbatim it makes every `parcel_id` ugly
>   (`nyc:1008350041.00000000`). Added `_clean_bbl()`.
> - **Miami (`PaGISView_gdb`)**: `BLDG_ACTUAL_AREA` is really `BUILDING_ACTUAL_AREA`. There
>   is no `MUNICIPALITY`/`MUNIC_NAME` field — the municipality is `TRUE_SITE_CITY`. Worse:
>   `M21_Zoning` is not a layer on the county's ArcGIS org at all (`Invalid URL`) — the City
>   of Miami's Miami 21 zoning lives on the CITY's own GIS server, as a **MapServer** (not
>   FeatureServer) at `gis.miami.gov/gis/rest/services/Zoning/ZoningMiami21/MapServer/5`,
>   field `M21_ZONE` (the plan's guessed field name, `ZONE`, doesn't exist). Also: house
>   numbers on numbered streets lose their ordinal in `TRUE_SITE_ADDR` ("2801 NW 2 AVE",
>   never "2ND") — the brief's own test address, "2618 NW 2nd Ave", doesn't exist in the PA
>   database at all (confirmed by direct query, not just our LIKE filter) and had to be
>   swapped for a real Wynwood folio.
> - **LA (`LACounty_Parcel`)**: there is no flat `YearBuilt`/`SQFTmain`/`Units` column — a
>   parcel can carry up to 5 structures, so every one of those is numbered `1`..`5`
>   (`YearBuilt1`, `SQFTmain1`, `Units1`, ...); we read design 1. There is also no
>   standalone lot-size attribute at all — the fix reads the ArcGIS-computed
>   `Shape.STArea()` (returned free by `outFields=*`), the parcel polygon's own area.
> - **Chicago**: the deepest drift, three datasets deep. `3723-97qp`'s real address column
>   is `prop_address_full`, not `property_address` — AND that dataset already carries
>   `owner_address_name`/`mail_address_name`, meaning the owner join the plan expected from
>   the attrs dataset lives here instead. `pabr-t5kh` ("Parcel Universe") turned out to be
>   pure geographic/tax-district reference data (township, census tract, school district) —
>   it has no year/sqft/stories/owner columns of any kind; the real building-characteristics
>   dataset is `x54s-btds` ("Single and Multi-Family Improvement Characteristics"), keyed by
>   `char_yrblt` / `char_bldg_sf` / `char_land_sf` / `char_type_resd` (a STRING like
>   `"3 Story +"`, not a number — floors is parsed from its leading digit). And `7cve-jgbp`
>   is an `assetType: "map"` visualization asset with zero queryable SODA rows
>   (`$select=*` returns `{}`) — the real tabular resource behind it is `dj47-wfun` (the
>   plan's guessed field name, `zone_class`, was correct; only the dataset ID was wrong).
>
> `tests/test_registry.py` also needed updating: its Task-7-era assertions
> (`test_parcel_provider_is_none_for_every_metro_until_task_9`, and the non-NYC geocoder
> test) asserted `None` because no `parcel_*` module existed yet. Landing this task made
> both assertions fail for the right reason — they were testing the ABSENCE of what this
> task builds. Replaced with `test_parcel_provider_is_the_matching_module_for_every_metro`
> and updated the geocoder test to assert delegation, not `None`.

- [ ] **Step 1: Add parcel persistence to `db.py`**

```python
def save_parcel(p) -> str:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO parcel (parcel_id, metro, owner_name, zoning, far_built, far_allowed,"
            " year_built, lot_sqft, bldg_sqft, floors, units, use_code, missing_reason_json, raw_json)"
            " VALUES (:parcel_id, :metro, :owner_name, :zoning, :far_built, :far_allowed,"
            " :year_built, :lot_sqft, :bldg_sqft, :floors, :units, :use_code, :missing_reason_json, :raw_json)"
            " ON CONFLICT(parcel_id) DO UPDATE SET"
            " owner_name=excluded.owner_name, zoning=excluded.zoning, far_built=excluded.far_built,"
            " far_allowed=excluded.far_allowed, year_built=excluded.year_built, lot_sqft=excluded.lot_sqft,"
            " bldg_sqft=excluded.bldg_sqft, floors=excluded.floors, units=excluded.units,"
            " use_code=excluded.use_code, missing_reason_json=excluded.missing_reason_json,"
            " raw_json=excluded.raw_json, fetched_at=datetime('now')",
            {**p.model_dump(exclude={"missing_reason"}),
             "missing_reason_json": json.dumps(p.missing_reason)},
        )
    return p.parcel_id


def get_parcel(parcel_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM parcel WHERE parcel_id = ?", (parcel_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["missing_reason"] = json.loads(d.pop("missing_reason_json") or "{}")
    return d
```

- [ ] **Step 2: Write `providers/parcel_nyc.py`**

```python
"""NYC — PLUTO on Socrata, joined by BBL (from GeoSearch). The one gotcha: `bbl` is a
NUMBER column, so it must be filtered UNQUOTED (`?bbl=1000160100`, not `?bbl='1000…'`) or
Socrata returns nothing at all. PLUTO refreshes ~2x/year, so a parcel is cached forever.

Verified live 2026-07-12: PLUTO's own `bbl` column in the JSON response is NOT the clean
10-digit string GeoSearch hands us — Socrata serializes this NUMBER column with full
decimal precision, e.g. "1008350041.00000000". Used verbatim that turns every parcel_id
into `nyc:1008350041.00000000`; _clean_bbl() below strips it back to the integer BBL."""
import json

import httpx

from ..cache import cached
from ..models import Parcel
from . import geosearch

SOCRATA = "https://data.cityofnewyork.us/resource/64uk-42ks.json"


def _clean_bbl(v) -> str:
    return str(int(float(v)))


def normalize(raw: dict) -> Parcel:
    def num(k, cast=float):
        """Socrata serializes numerics as decimal STRINGS, inconsistently: PLUTO gives
        numfloors as "102.0000000" but yearbuilt as "1931". `int("102.0000000")` RAISES,
        the except swallows it, and `floors` comes back None -- which the listing page then
        renders as "not published in this market", for a field NYC publishes on every lot.
        A silently-dropped field is a WRONG answer. Go through float() first."""
        v = raw.get(k)
        try:
            return cast(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Parcel(
        parcel_id=f"nyc:{_clean_bbl(raw['bbl'])}", metro="nyc",
        owner_name=raw.get("ownername") or None,
        zoning=raw.get("zonedist1") or None,
        far_built=num("builtfar"), far_allowed=num("commfar") or num("residfar"),
        year_built=num("yearbuilt", int), lot_sqft=num("lotarea", int),
        bldg_sqft=num("bldgarea", int), floors=num("numfloors", int),
        units=num("unitstotal", int), use_code=raw.get("landuse") or None,
        raw_json=json.dumps(raw),
    )


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    g = geosearch.geocode(address)
    if not g or not g.get("bbl"):
        return None
    bbl = g["bbl"]

    def fetch():
        r = httpx.get(SOCRATA, params={"bbl": bbl}, timeout=30.0)  # UNQUOTED — it's a NUMBER col
        r.raise_for_status()
        return r.json()

    rows = cached("pluto", "bbl", {"bbl": bbl}, fetch)
    return normalize(rows[0]) if rows else None
```

- [ ] **Step 3: Write `providers/parcel_miami.py`**

```python
"""Miami-Dade — the county Property Appraiser's ArcGIS FeatureServer, joined by 13-digit
folio.

The trap: the COUNTY zoning layer returns ZERO features for Brickell, Wynwood and
Downtown, because those are incorporated cities that zone themselves. A naive read of that
zero looks like "no zoning" and would silently blank the field for the exact neighborhoods
the app is most used in. So we branch to the municipal layer when the parcel is inside a
known city, and when we have no branch for a municipality we return zoning=None WITH the
reason — never an empty string.

Verified live 2026-07-12 (plan said otherwise on three counts — see docs/implementation-plan.md
Task 9 correction):
  - The PA layer's actual field is `BUILDING_ACTUAL_AREA`, not `BLDG_ACTUAL_AREA`.
  - There is no `MUNICIPALITY`/`MUNIC_NAME` field at all — the municipality the parcel sits
    in is `TRUE_SITE_CITY`.
  - `M21_Zoning` is not a layer on the county's ArcGIS org — the City of Miami's zoning
    (Miami 21) is hosted on the CITY's own GIS server, a MapServer (not FeatureServer) at
    `gis.miami.gov/.../ZoningMiami21/MapServer/5`, and the zone-code field is `M21_ZONE`
    (brief guessed `ZONE`). House-numbered street addresses also lose their ordinal suffix
    in TRUE_SITE_ADDR ("2801 NW 2 AVE", never "2ND") — stripped before the LIKE query."""
import json
import re

import httpx

from ..cache import cached
from ..models import Parcel

PA = ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/"
      "PaGISView_gdb/FeatureServer/0/query")
# City of Miami covers Brickell / Wynwood / Downtown / Little Havana / Coconut Grove.
MUNI_ZONING = {
    "MIAMI": ("https://gis.miami.gov/gis/rest/services/Zoning/ZoningMiami21/MapServer/5/query",
              "M21_ZONE"),
}
NO_BRANCH = ("Zoning here is set by the municipality, and OpenLease has no layer wired "
             "for it yet. The county layer covers unincorporated Miami-Dade only.")
_ORDINAL = re.compile(r"(\d+)(ST|ND|RD|TH)\b")  # Miami-Dade's addressing drops ordinals


def normalize(raw: dict, zoning: str | None = None,
              zoning_reason: str | None = None) -> Parcel:
    def num(k, cast=float):
        v = raw.get(k)
        try:
            return cast(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    missing = {}
    if zoning is None:
        missing["zoning"] = zoning_reason or NO_BRANCH
    return Parcel(
        parcel_id=f"mia:{raw['FOLIO']}", metro="mia",
        owner_name=raw.get("TRUE_OWNER1") or None,
        zoning=zoning,
        year_built=num("YEAR_BUILT", int), lot_sqft=num("LOT_SIZE", int),
        bldg_sqft=num("BUILDING_ACTUAL_AREA", int), floors=num("FLOOR_COUNT", int),
        units=num("UNIT_COUNT", int), use_code=raw.get("DOR_DESC") or None,
        missing_reason=missing, raw_json=json.dumps(raw),
    )


def _zoning(muni: str, lat: float, lng: float) -> tuple[str | None, str | None]:
    entry = MUNI_ZONING.get((muni or "").upper())
    if not entry:
        return None, NO_BRANCH
    url, field = entry

    def fetch():
        r = httpx.get(url, params={
            "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": field, "returnGeometry": "false", "f": "json"}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("miami_zoning", muni, {"lat": round(lat, 6), "lng": round(lng, 6)}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None, f"No zoning polygon covers this point in the {muni} layer."
    return feats[0]["attributes"].get(field), None


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    street = _ORDINAL.sub(r"\1", address.split(",")[0].upper())

    def fetch():
        r = httpx.get(PA, params={
            "where": f"TRUE_SITE_ADDR LIKE '{street}%'",
            "outFields": "*", "returnGeometry": "false", "resultRecordCount": 1,
            "f": "json"}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("miami_pa", "address", {"addr": street}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None
    raw = feats[0]["attributes"]
    muni = raw.get("TRUE_SITE_CITY") or ""
    z, reason = (_zoning(muni, lat, lng) if lat and lng else (None, NO_BRANCH))
    return normalize(raw, z, reason)
```

- [ ] **Step 4: Write `providers/parcel_la.py`**

```python
"""LA County Assessor, joined by 10-digit AIN.

There is NO owner name, and there never will be: California statute does not make
owner-of-record free and public through the county's open GIS. An LA listing therefore
shows fewer fields BY DESIGN. That is a documented `missing_reason`, not a bug and not a
scraping opportunity — if the UI ever renders a blank there instead of the reason, the app
is lying about what it knows.

Verified live 2026-07-12 (plan said otherwise — see docs/implementation-plan.md Task 9
correction): there is no flat `YearBuilt`/`SQFTmain`/`Units` column. A parcel can carry up
to 5 separate structures ("designs"), so the Assessor numbers every building field
`YearBuilt1..5` / `SQFTmain1..5` / `Units1..5`; we read design 1 (the primary structure).
There is also no standalone lot-size attribute — `outFields=*` already returns the
ArcGIS-computed `Shape.STArea()` (the parcel polygon's own area, in the service's native
square feet), which is the only honest source for `lot_sqft` here."""
import json

import httpx

from ..cache import cached
from ..models import Parcel

MAPSERVER = ("https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/"
             "LACounty_Parcel/MapServer/0/query")
OWNER_REASON = ("California statute: owner-of-record is not published free through the "
                "county's open GIS. This is a gap in the public data, not a failed lookup.")
ZONING_REASON = "LA zoning lives in a separate county layer; not wired in v1."


def normalize(raw: dict) -> Parcel:
    def num(k, cast=float):
        v = raw.get(k)
        try:
            return cast(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Parcel(
        parcel_id=f"la:{raw['AIN']}", metro="la",
        owner_name=None,                      # never available — see OWNER_REASON
        zoning=None,
        year_built=num("YearBuilt1", int), lot_sqft=num("Shape.STArea()", int),
        bldg_sqft=num("SQFTmain1", int), units=num("Units1", int),
        use_code=raw.get("UseType") or raw.get("UseDescription") or None,
        missing_reason={"owner_name": OWNER_REASON, "zoning": ZONING_REASON},
        raw_json=json.dumps(raw),
    )


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    where = (f"SitusFullAddress LIKE '{address.split(',')[0].upper()}%'" if not lat
             else "1=1")
    params = {"where": where, "outFields": "*", "returnGeometry": "false",
              "resultRecordCount": 1, "f": "json"}
    if lat and lng:
        params |= {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
                   "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}

    def fetch():
        r = httpx.get(MAPSERVER, params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("la_parcel", "query", {"addr": address, "lat": lat, "lng": lng}, fetch)
    feats = data.get("features") or []
    return normalize(feats[0]["attributes"]) if feats else None
```

- [ ] **Step 5: Write `providers/parcel_chicago.py`**

```python
"""Cook County (parcel, by 14-digit PIN) + City of Chicago (zoning).

The trap: zoning, floors and FAR come from a CITY OF CHICAGO dataset, so they are NULL for
roughly half of Cook County — every suburb. A suburban parcel with zoning="" would read as
"unzoned", which is nonsense. Return None with the reason instead.

Verified live 2026-07-12 (plan said otherwise on three counts — see docs/implementation-plan.md
Task 9 correction):
  - `3723-97qp` ("Assessor - Parcel Addresses") has no `property_address` column; the real
    column is `prop_address_full`, and it also carries `owner_address_name` — the owner join
    the plan expected to come from the attrs dataset actually lives HERE.
  - `pabr-t5kh` ("Assessor - Parcel Universe") is geographic/tax-district reference data
    (township, census tract, school district...) — it has no year/sqft/stories/owner at all.
    Building characteristics live in `x54s-btds` ("Assessor - Single and Multi-Family
    Improvement Characteristics"), keyed by `char_*` columns.
  - `7cve-jgbp` is a "map" visualization asset (assetType=map) with no queryable SODA rows
    (`$select=*` returns `{}`). The real underlying tabular resource is `dj47-wfun`; the
    `zone_class` field name the plan guessed was otherwise correct.
"""
import json
import re

import httpx

from ..cache import cached
from ..models import Parcel

ADDR = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"   # address -> PIN + owner
ATTRS = "https://datacatalog.cookcountyil.gov/resource/x54s-btds.json"  # PIN -> characteristics
CITY_ZONING = "https://data.cityofchicago.org/resource/dj47-wfun.json"
SUBURB_REASON = ("Zoning is a City of Chicago dataset. This parcel is in suburban Cook "
                 "County, which the city does not zone — the data does not exist, the "
                 "lookup did not fail.")
_STORY_RE = re.compile(r"(\d+)")


def normalize(raw: dict, zoning: str | None = None,
              zoning_reason: str | None = None) -> Parcel:
    def num(k, cast=float):
        # Cook County serializes every numeric column as a decimal STRING ("1972.0",
        # "5742.0", never a clean int) — go through float() first or int("1972.0") raises.
        v = raw.get(k)
        try:
            return cast(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def floors():
        # char_type_resd is a descriptive string ("2 Story", "3 Story +"), not a number —
        # the leading digit IS the story count; this reads it, it does not guess it.
        m = _STORY_RE.match(raw.get("char_type_resd") or "")
        return int(m.group(1)) if m else None

    missing = {}
    if zoning is None:
        missing["zoning"] = zoning_reason or SUBURB_REASON
    return Parcel(
        parcel_id=f"chi:{raw['pin']}", metro="chi",
        owner_name=raw.get("owner_address_name") or raw.get("mail_address_name") or None,
        zoning=zoning,
        year_built=num("char_yrblt", int), lot_sqft=num("char_land_sf", int),
        bldg_sqft=num("char_bldg_sf", int), floors=floors(),
        units=None,   # no clean numeric unit count is published here (char_apts is a word
                      # like "Six") — None because it genuinely isn't parseable, not a fake.
        use_code=raw.get("class") or None,   # Cook's `class` = building class + land use
        missing_reason=missing, raw_json=json.dumps(raw),
    )


def _zoning(lat: float, lng: float) -> tuple[str | None, str | None]:
    def fetch():
        r = httpx.get(CITY_ZONING, params={
            "$where": f"intersects(the_geom, 'POINT ({lng} {lat})')", "$limit": 1},
            timeout=30.0)
        r.raise_for_status()
        return r.json()

    rows = cached("chi_zoning", "point", {"lat": round(lat, 6), "lng": round(lng, 6)}, fetch)
    if not rows:
        return None, SUBURB_REASON
    return rows[0].get("zone_class"), None


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    street = address.split(",")[0].upper()

    def fetch_pin():
        r = httpx.get(ADDR, params={"$where": f"upper(prop_address_full) like '{street}%'",
                                    "$order": "year DESC", "$limit": 1}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    hits = cached("cook_addr", "search", {"addr": street}, fetch_pin)
    if not hits:
        return None
    addr_row = hits[0]
    pin = addr_row.get("pin") or addr_row.get("pin10")

    def fetch_attrs():
        r = httpx.get(ATTRS, params={"pin": pin, "$order": "year DESC", "$limit": 1},
                       timeout=30.0)
        r.raise_for_status()
        return r.json()

    rows = cached("cook_attrs", "pin", {"pin": pin}, fetch_attrs)
    raw = {**addr_row, **(rows[0] if rows else {}), "pin": pin}
    z, reason = (_zoning(lat, lng) if lat and lng else (None, SUBURB_REASON))
    return normalize(raw, z, reason)
```

- [ ] **Step 6: Capture the parcel fixtures**

```bash
.venv/bin/python -c "
import json
from app.providers import parcel_nyc, parcel_miami, parcel_la, parcel_chicago
cases = [
    ('nyc', parcel_nyc, '350 5th Ave, New York, NY', 40.7484, -73.9857),
    ('mia', parcel_miami, '2618 NW 2nd Ave, Miami, FL', 25.8015, -80.1993),
    ('la',  parcel_la, '8000 Melrose Ave, Los Angeles, CA', 34.0836, -118.3639),
    ('chi', parcel_chicago, '1550 N Damen Ave, Chicago, IL', 41.9101, -87.6773),
]
for name, mod, addr, lat, lng in cases:
    p = mod.lookup(addr, lat, lng)
    print(name, '->', p.parcel_id if p else 'NO MATCH', '| owner:', p.owner_name if p else '-',
          '| zoning:', p.zoning if p else '-')
    if p:
        json.dump(json.loads(p.raw_json), open(f'tests/fixtures/parcel_{name}.json','w'))
"
```

Expected: NYC returns an owner and a zoning district; Miami returns an owner and a City of
Miami zoning code (Wynwood is inside the city — this is the branch that the county layer
would have blanked); LA returns `owner: None`; Chicago returns an owner and a city zone
class. Any `NO MATCH` means an address-search field name moved — fix the provider, not the
test.

> **Reality (2026-07-12): two of the four seed addresses above are `NO MATCH` — for real,
> confirmed-by-direct-query reasons, not a fixable field-name bug.** "2618 NW 2nd Ave,
> Miami, FL" (`seed.py`'s Wynwood demo listing) does not exist anywhere in the Miami-Dade PA
> database — querying `TRUE_SITE_ADDR LIKE '2618 NW 2%'` returns zero rows, confirmed by
> direct ArcGIS query, not just this provider's LIKE clause. "1550 N Damen Ave, Chicago, IL"
> (`seed.py`'s Wicker Park listing) is likewise absent from `3723-97qp`. Both are almost
> certainly fictional/rounded demo addresses that were never checked against the real
> parcel databases when `seed.py` was written. The fixtures actually captured use real,
> nearby, verified addresses instead — **`2801 NW 2 Ave, Miami, FL`** (a real Wynwood folio,
> still inside the City of Miami branch this step exists to demonstrate — zoning came back
> `T5-O` from `ZoningMiami21`) and **`2257 N Kedzie Ave, Chicago, IL`** (a real Logan Square
> PIN with complete owner/characteristics data — zoning `RT-4`). This is a `seed.py`
> data-quality finding, not a Task 9 defect; `seed.py` is out of this task's file list and
> was left as-is (`tests/test_smoke.py`'s assertions on the literal seed addresses still pass —
> see the T9 note added there disabling live parcel lookups for that test).

- [ ] **Step 7: Write the test**

`tests/test_parcel.py`:

```python
"""Field normalization per metro, and the invariant that matters more than any of them:
a field this market does not publish is None WITH A REASON — never 0, never "", never
confused with a failed lookup."""
import json
import os
import pathlib
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_parcel.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.providers import parcel_chicago, parcel_la, parcel_miami, parcel_nyc  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    p = FIX / f"parcel_{name}.json"
    if not p.exists():
        pytest.skip(f"{p.name} not captured — see Task 9 Step 6")
    return json.loads(p.read_text())


def test_nyc_normalizes_pluto():
    p = parcel_nyc.normalize(_fx("nyc"))
    assert p.metro == "nyc" and p.parcel_id.startswith("nyc:")
    assert p.owner_name and p.zoning          # NYC publishes both
    assert p.year_built and p.lot_sqft
    assert p.missing_reason == {}


def test_la_owner_is_none_with_a_reason_not_a_failure():
    p = parcel_la.normalize(_fx("la"))
    assert p.owner_name is None
    assert "California statute" in p.missing_reason["owner_name"]
    assert p.parcel_id.startswith("la:")
    assert p.lot_sqft is not None             # the fields LA DOES publish still land


def test_miami_zoning_null_outside_a_wired_municipality():
    raw = _fx("mia")
    with_zone = parcel_miami.normalize(raw, "T6-8-O")
    assert with_zone.zoning == "T6-8-O" and with_zone.missing_reason == {}
    without = parcel_miami.normalize(raw)     # no municipal branch -> null + reason
    assert without.zoning is None
    assert "municipality" in without.missing_reason["zoning"]


def test_chicago_zoning_null_in_the_suburbs():
    p = parcel_chicago.normalize(_fx("chi"))  # no zoning passed = the suburban path
    assert p.zoning is None
    assert "suburban Cook" in p.missing_reason["zoning"]
    assert p.parcel_id.startswith("chi:")


def test_no_metro_ever_fakes_a_zero():
    for mod, name in [(parcel_nyc, "nyc"), (parcel_miami, "mia"),
                      (parcel_la, "la"), (parcel_chicago, "chi")]:
        p = mod.normalize(_fx(name))
        for field in ("owner_name", "zoning", "year_built", "lot_sqft", "bldg_sqft", "floors"):
            v = getattr(p, field)
            assert v is None or v != 0, f"{name}.{field} is a zero — that is a lie, use None"


def test_parcel_round_trips_through_sqlite():
    db.init_db()
    p = parcel_la.normalize(_fx("la"))
    pid = db.save_parcel(p)
    got = db.get_parcel(pid)
    assert got["owner_name"] is None
    assert "California statute" in got["missing_reason"]["owner_name"]   # the reason SURVIVES
```

- [ ] **Step 8: Wire the parcel into the listing page**

In `routes_listings.py`, replace the `"parcel": None` line in `listing_page` with a lazy,
cached lookup:

```python
    parcel = None
    if row.get("parcel_id"):
        parcel = db.get_parcel(row["parcel_id"])
    else:
        from . import registry
        prov = registry.parcel_provider(row["metro"])
        if prov:
            try:
                p = prov.lookup(row["address"], row.get("lat"), row.get("lng"))
            except Exception as e:  # noqa: BLE001 — a parcel API being down must not 500 the page
                import logging
                logging.getLogger("openlease").warning(
                    "parcel lookup failed for listing %s (%s): %s", listing_id, type(e).__name__, e)
                p = None
            if p:
                db.save_parcel(p)
                with db.get_conn() as conn:
                    conn.execute("UPDATE listing SET parcel_id = ? WHERE id = ?",
                                 (p.parcel_id, listing_id))
                parcel = db.get_parcel(p.parcel_id)
```

and pass `parcel` (not `None`) into the template context.

> **Reality (2026-07-12): this makes `/listings/{id}` a live-network route for the first
> view of any listing whose metro has a real `ParcelProvider` — which, as of this task, is
> all four.** Two pre-existing `tests/test_smoke.py` cases (`test_search_ui_and_listing_page`,
> `test_listing_page_links_real_broker_source_url`) hit `/listings/{id}` for a Miami seed
> listing with no `parcel_id` yet set. Without a guard, landing this step would make BOTH
> of them fire a real HTTP request at the Miami-Dade PA ArcGIS endpoint on every suite run
> — exactly what "hermetic, no live network calls" forbids, and a silent regression neither
> test's assertions would have caught (they don't look at the Parcel panel). Both now
> `monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)` before calling the
> route; parcel behavior itself is covered end-to-end by `tests/test_parcel.py`.

- [ ] **Step 9: Run to green and look at the LA listing**

```bash
.venv/bin/python -m pytest tests/ -v && ./run.sh
```

Open the Melrose (LA) listing. Expected: the Parcel panel shows the fields LA publishes and,
for Owner, the italic **"not published here"** with the California-statute reason on hover —
not a blank row. Ctrl-C when done.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(openlease): parcel providers x4 — PLUTO/Miami-Dade/LA/Cook, null-with-a-reason never a fake zero"
```

---

### Task 10: The fetch ladder — `crawl.py` + `extract.py` + `sources.yml`

The moat. **One generic ladder, not 13 scrapers.** Descend only when the rung above is
absent: robots → sitemap → structured feed → HTML+LLM. A site redesign costs nothing; a
new site is a URL in a YAML file.

Read the Global Constraints again before starting this task. The guardrails there are not
style: they are the difference between the hiQ-*protected* case and the CoStar-v-CREXi
one.

**Files:**
- Create: `app/crawl.py`, `app/extract.py`, `app/data/sources.yml`, `app/routes_crawl.py`
- Modify: `app/app.py` (route import), `app/db.py` (append `crawl_log`)
- Test: `tests/test_extract.py` + `tests/fixtures/{ripco_wpjson.json,jsonld_listing.html}`

**Interfaces:**
- Consumes: `db.save_listing` (T2), `ai` (T4), `score.enrich` (T8), `settings` (T1).
- Produces:
  - `extract.from_wp_json(item: dict, src: dict) -> dict | None`
  - `extract.from_jsonld(html: str, url: str, src: dict) -> dict | None`
  - `extract.from_html_llm(markdown: str, url: str, src: dict) -> dict | None`
  - `extract.ListingExtract` (the `messages.parse` schema — all-required sentinels, same rules as T4)
  - `crawl.allowed(url: str) -> bool` — robots.txt, fetched and obeyed
  - `crawl.crawl_source(src: dict, limit: int = 100) -> list[dict]`
  - `crawl.run(metro: str | None = None, limit: int = 100) -> dict` — `{"fetched", "saved", "skipped", "errors"}`
  - `POST /api/crawl`

> **Correction (Task 10):** the three `extract.from_*` signatures above are missing a
> parameter — the actual Step 3 code (and every call site in Step 4's `crawl_source`)
> takes a 4th positional `metro: str`, e.g. `from_wp_json(item: dict, src: dict, metro:
> str) -> dict | None`. It has to: `_clean()` stamps `d["metro"] = metro` on every
> extracted record, and `metro` is a `NOT NULL` column (`db.py` SCHEMA) — there is no
> other source for it once the fixed feed URL / detail page no longer carries the metro
> in its own data. Interface list corrected to match the code below.

> **Correction (Task 10 fix pass — review findings):** the same drift exists one line
> down and was missed the first time: `crawl.crawl_source(src: dict, limit: int = 100)`
> above is also missing a parameter. The actual signature (Step 4's code, and the only
> way `run()` ever calls it) is `crawl_source(src: dict, metro: str, limit: int = 100) ->
> list[dict]` — for the identical reason as the `extract.from_*` correction above:
> `metro` has no other source once inside the function. Corrected interface:
> `crawl.crawl_source(src: dict, metro: str, limit: int = 100) -> list[dict]`.

- [ ] **Step 1: Write `data/sources.yml`**

The allowlist **is** the crawler's scope. A site not in this file is never fetched. Each
entry records which rung of the ladder it sits on and what robots.txt said when it was
verified — so a future reader can tell "we chose not to" from "we never checked."

```yaml
# The per-domain allowlist. A domain not listed here is NEVER fetched.
# rung: feed_wp | jsonld | html      (the ladder stops at the highest rung that works)
# tier: default | stealth            (stealth = Cloudflare/Vercel-walled; needs `scrapling install`)
# Verified 2026-07-11 (spec §7). robots: what robots.txt said THEN — crawl.py re-checks
# it every run and obeys the live file, not this note.
#
# NON-NEGOTIABLE: no entry here may require a login, an account, or a registration/NDA
# gate. Stealth defeats a bot-detection WAF on a PUBLIC page; it never crosses an auth
# wall. If a site starts requiring an account, DELETE it from this file.

nyc:
  - key: ripco
    name: RIPCO Real Estate
    url: https://www.ripcony.com
    feed: https://www.ripcony.com/wp-json/wp/v2/property-listings?per_page=100
    rung: feed_wp        # 833 listings, structured, no auth, no scraping at all
    tier: default
    robots: allows /wp-json
  - key: rtl
    name: RTL Real Estate
    url: https://www.rtl-re.com/listings
    rung: jsonld
    tier: default
  - key: metro_manhattan
    name: Metro Manhattan Office Space
    url: https://www.metro-manhattan.com
    rung: jsonld
    tier: default
  - key: ksr
    name: KSR NY
    url: https://www.ksrny.com
    rung: html
    tier: stealth        # 429-throttles aggressively; crawl_delay is doubled below
    crawl_delay: 8
  - key: nycretail
    name: NYC Retail Leasing
    url: https://www.nycretailleasing.com
    rung: html
    tier: stealth        # 403 to a plain fetch

mia:
  - key: metro1
    name: Metro 1
    url: https://metro1.com/listings
    rung: jsonld
    tier: default
  - key: comras
    name: The Comras Company
    url: https://www.comras.com
    rung: html
    tier: default
  - key: terranova
    name: Terranova Corporation
    url: https://www.terranovacorp.com
    rung: html
    tier: default
  - key: ripco_mia
    name: RIPCO (Miami)
    url: https://www.ripcony.com
    feed: https://www.ripcony.com/wp-json/wp/v2/property-listings?per_page=100&locations=Miami
    rung: feed_wp
    tier: default

la:
  - key: avison_la
    name: Avison Young (LA)
    url: https://www.avisonyoung.us/web/los-angeles/properties-for-lease
    rung: html
    tier: default        # server-rendered
  - key: rexford
    name: Rexford Industrial
    url: https://www.rexfordindustrial.com/properties
    rung: jsonld
    tier: default
  - key: westmac
    name: WESTMAC Commercial
    url: https://www.westmac.com/listings
    rung: html
    tier: default

chi:
  - key: avison_chi
    name: Avison Young (Chicago)
    url: https://www.avisonyoung.us/web/chicago/properties-for-lease
    rung: html
    tier: default
  - key: midamerica
    name: Mid-America Real Estate
    url: https://www.midamericagrp.com/property-listings?saleOrLease=lease
    rung: html
    tier: default
  - key: baum
    name: Baum Realty Group
    url: https://www.baumrealty.com/properties
    rung: html
    tier: default
  - key: svn_chi
    name: SVN Chicago
    url: https://www.svnchicago.com/properties
    rung: html
    tier: default
```

- [ ] **Step 2: Append `crawl_log` to `db.SCHEMA`**

```sql
-- Per-domain daily budget + conditional-GET bookkeeping. The cap is enforced from here,
-- so a restart cannot reset it.
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY,
    domain      TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      INTEGER,
    etag        TEXT,
    last_mod    TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_crawl_domain_day ON crawl_log(domain, fetched_at);
```

- [ ] **Step 3: Write `extract.py`**

```python
"""Feed/HTML -> a normalized Listing dict. THREE fast paths and one fallback, in order:

  1. WordPress REST  — the big win. RIPCO alone publishes 833 listings as clean JSON at
     /wp-json/wp/v2/property-listings. No scraping at all.
  2. JSON-LD         — <script type="application/ld+json"> on the detail page.
  3. HTML + LLM      — last resort. Scrapling's Convertor strips the container to markdown
                       and ONE prompt maps it to the schema. NO per-site CSS parsers: a
                       redesign costs nothing, and a new site is a URL in sources.yml.

Whatever the path, we store FACTS, never expression:
  - `our_description` is written by US from the facts. The broker's marketing prose is
    never persisted — we link `source_url` for the original.
  - `photo_urls` are the broker's own URLs, referenced. Never downloaded, never re-hosted.
"""
import json
import logging
import re

from pydantic import BaseModel

from . import ai
from .config import settings

log = logging.getLogger("openlease")

_TYPES = ("retail", "office", "industrial", "flex", "land")


class ListingExtract(BaseModel):
    """Same two rules as ai.QueryExtract, for the same two reasons: no `| None` (>16
    union params = 400) and no defaults (any optional param = a 2^N grammar = the request
    HANGS). Sentinels: "" / 0 mean the page didn't say."""
    address: str
    neighborhood: str
    property_type: str        # retail | office | industrial | flex | land | ""
    transaction_type: str     # lease | sale | ""
    size_sf: int
    divisible_min_sf: int
    divisible_max_sf: int
    floor: str
    ceiling_height_ft: float
    asking_rent: float
    rent_unit: str            # sf_yr | sf_mo | mo | ""
    lease_type: str
    sale_price: int
    availability_date: str
    broker_name: str
    broker_firm: str
    broker_phone: str
    broker_email: str
    features: list[str]
    our_description: str      # OUR words, from the facts — NOT the page's marketing copy

    def to_listing(self) -> dict:
        d = {k: v for k, v in self.model_dump().items() if v not in ("", 0, 0.0, [])}
        if "features" in d:
            d["features_json"] = json.dumps(d.pop("features"))
        return d


def _clean(d: dict, src: dict, url: str, metro: str) -> dict | None:
    if not d.get("address"):
        return None
    d["source"] = src["key"]
    d["source_url"] = url
    d["metro"] = metro
    d.setdefault("transaction_type", "lease")
    if d.get("property_type") not in _TYPES:
        d.pop("property_type", None)
    return d


# --- rung 3a: WordPress REST --------------------------------------------------

def from_wp_json(item: dict, src: dict, metro: str) -> dict | None:
    """WP custom-post-type listing. Field names vary by theme, so we look in the usual
    places and let the LLM description step fill the gaps — never a per-site parser."""
    meta = item.get("acf") or item.get("meta") or {}
    title = (item.get("title") or {}).get("rendered", "") if isinstance(item.get("title"), dict) \
        else (item.get("title") or "")
    title = re.sub(r"<[^>]+>", "", title).strip()

    def pick(*keys):
        for k in keys:
            v = meta.get(k) or item.get(k)
            if v not in (None, "", []):
                return v
        return None

    def num(v, cast=int):
        if v is None:
            return None
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        try:
            return cast(m.group()) if m else None
        except (TypeError, ValueError):
            return None

    d = {
        "address": pick("address", "property_address", "street_address") or title,
        "neighborhood": pick("neighborhood", "submarket"),
        "property_type": (str(pick("property_type", "type") or "").lower() or None),
        "size_sf": num(pick("size", "square_feet", "sf", "total_sf")),
        "divisible_min_sf": num(pick("divisible_min", "min_sf")),
        "divisible_max_sf": num(pick("divisible_max", "max_sf")),
        "asking_rent": num(pick("asking_rent", "rent", "price_per_sf"), float),
        "rent_unit": "sf_yr" if pick("asking_rent", "rent", "price_per_sf") else None,
        "broker_name": pick("broker", "agent", "contact_name"),
        "broker_phone": pick("phone", "contact_phone"),
        "broker_email": pick("email", "contact_email"),
        "broker_firm": src["name"],
        # photos: the broker's own URLs, hot-linked. NEVER downloaded.
        "photo_urls_json": json.dumps([
            u for u in [pick("featured_image", "image", "thumbnail")] if isinstance(u, str)
        ]) or None,
    }
    d = {k: v for k, v in d.items() if v is not None}
    d = _clean(d, src, item.get("link") or item.get("guid", {}).get("rendered", ""), metro)
    if d:
        d["our_description"] = describe(d)      # our words, not the post's content field
    return d


# --- rung 3b: JSON-LD ---------------------------------------------------------

_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                 re.S | re.I)


def from_jsonld(html: str, url: str, src: dict, metro: str) -> dict | None:
    for blob in _LD.findall(html):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            addr = node.get("address")
            if isinstance(addr, dict):
                street = addr.get("streetAddress")
                city = addr.get("addressLocality")
            else:
                street, city = (addr if isinstance(addr, str) else None), None
            if not street:
                continue
            offer = node.get("offers") or {}
            size = node.get("floorSize") or {}
            d = {
                "address": f"{street}, {city}" if city else street,
                "neighborhood": city,
                "size_sf": int(size.get("value")) if str(size.get("value", "")).isdigit() else None,
                "asking_rent": float(offer["price"]) if str(offer.get("price", "")).replace(".", "").isdigit() else None,
                "rent_unit": "sf_yr" if offer.get("price") else None,
                "photo_urls_json": json.dumps(
                    [node["image"]] if isinstance(node.get("image"), str) else (node.get("image") or [])
                ),
                "broker_firm": src["name"],
            }
            d = {k: v for k, v in d.items() if v is not None}
            d = _clean(d, src, url, metro)
            if d:
                d["our_description"] = describe(d)
            return d
    return None


# --- rung 4: HTML + one LLM prompt (no per-site parsers) ----------------------

def from_html_llm(markdown: str, url: str, src: dict, metro: str) -> dict | None:
    if not ai.available():
        log.warning("HTML rung needs ANTHROPIC_API_KEY — skipping %s. (The wp-json and "
                    "JSON-LD rungs still work keyless.)", url)
        return None
    try:
        resp = ai._client().messages.parse(
            model=settings.llm_model, max_tokens=2048,
            system=("Extract the ONE commercial space listed on this page into the schema. "
                    "Every field is required: use \"\" for text and 0 for numbers the page "
                    "does not state. Never invent a value.\n\n"
                    "our_description: write ONE original sentence describing the space FROM "
                    "THE FACTS (size, type, floor, location, features). Do NOT copy, quote, "
                    "or paraphrase the page's marketing copy — write your own."),
            messages=[{"role": "user", "content": markdown[:20000]}],
            output_format=ListingExtract,
        )
        return _clean(resp.parsed_output.to_listing(), src, url, metro)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extraction failed for %s (%s): %s", url, type(e).__name__, e)
        return None


def describe(d: dict) -> str:
    """One sentence, from the facts. Deterministic (keyless); the LLM rewrites it at
    ingest when a key is present. This exists so we NEVER need the broker's prose."""
    bits = []
    if d.get("size_sf"):
        bits.append(f"{d['size_sf']:,} SF")
    bits.append(d.get("property_type") or "commercial space")
    if d.get("floor"):
        bits.append(f"on floor {d['floor']}")
    if d.get("neighborhood"):
        bits.append(f"in {d['neighborhood']}")
    tail = ""
    if d.get("asking_rent"):
        tail = f", asking ${d['asking_rent']:,.0f}/SF/yr"
    return f"{' '.join(bits).capitalize()} at {d['address']}{tail}."
```

> **Correction (Task 10, verified with the JSON-LD fixture below):** `describe()`'s last
> line has a real bug — `str.capitalize()` upper-cases the FIRST character and
> **lower-cases every other character** in the string. Run against a real result
> (`"2,100 SF commercial space in Wicker Park"`), `.capitalize()` returns
> `"2,100 sf commercial space in wicker park"` — it silently destroys the "SF" unit
> abbreviation and any proper noun (a neighborhood name, here). This isn't cosmetic: T10's
> own test (`test_broker_prose_is_never_persisted`) asserts `"Wicker Park" in
> d["our_description"]`, which the literal code above fails (TDD RED, see
> `task-10-report.md`). Fixed in `app/extract.py` by upper-casing only the first character
> of the joined sentence (a no-op when that character is a digit, as it usually is here),
> never lower-casing the rest:
> ```python
> sentence = " ".join(bits)
> if sentence:
>     sentence = sentence[0].upper() + sentence[1:]
> return f"{sentence} at {d['address']}{tail}."
> ```

> **Correction (Task 10 fix pass — review findings): the HTML+LLM rung bypassed
> `cache.cached()` and the monthly budget cap entirely.** `from_html_llm` called
> `ai._client().messages.parse(...)` directly. 10 of 16 `sources.yml` entries are
> `rung: html` — with a key configured, a crawl over them spent real money with ZERO
> enforcement of `settings.monthly_budget_cents`, and re-billed in full on every re-crawl
> of the same page. This is the identical architecture rule Task 4's own review already
> found and fixed in `ai.py` ("every network call wrapped in `cache.cached()` with a
> monthly paid-spend cap" — `ai.py`'s docstring calls its two calls "the only paid
> surfaces in the app," which was no longer true once `extract.py` shipped).
>
> Fixed by wrapping the `messages.parse` call in `cache.cached("anthropic",
> "messages.parse.listing", req, fetch, cost_cents=_HTML_LLM_COST_CENTS)`
> (`_HTML_LLM_COST_CENTS = 5`, derived the same way `ai.py`'s own `_PARSE_COST_CENTS`/
> `_REPLY_COST_CENTS` are — see the comment above the constant in `app/extract.py`), and
> catching `cache.BudgetExceeded` separately from any other parse/API failure so the log
> names the budget specifically. A budget refusal returns `None` (no listing from that
> page this run) — it does not, and must not, crash the crawl. `ListingExtract` gained NO
> optional field in this change — every field stays required (see
> `test_html_llm_still_all_required_no_optional_field_added` in `tests/test_extract.py`;
> any optional param on a `messages.parse` schema makes the request HANG).
>
> **Two smaller findings from the same review, fixed in the same pass:**
> - `from_wp_json`'s address fallback used to be `pick(...) or title` — a WP post TITLE
>   is marketing copy ("280 Broadway – Ground Floor Retail!!"), not structured data, and
>   that bare `or title` could write the headline straight into the `address` FACT
>   column. Guarded: the title is only used as a fallback when it actually looks like a
>   street address (`_ADDR_LIKE = re.compile(r"^\d+\s+\S")` — starts with a house
>   number). A title that doesn't look address-shaped is simply not used, and the record
>   is dropped by `_clean()`'s existing `if not d.get("address"): return None` — same
>   safe failure mode as any other page missing an address.
> - `describe()`'s docstring claimed "the LLM rewrites it at ingest when a key is
>   present" — that rewrite pass does not exist anywhere in the codebase (`describe()`
>   IS `our_description` for the wp-json/JSON-LD rungs, key or no key). Corrected the
>   docstring rather than build an unrequested rewrite step; the HTML+LLM rung already
>   writes its own `our_description` directly, as part of `ListingExtract`, and never
>   calls `describe()` at all — the docstring now says so.

- [ ] **Step 4: Write `crawl.py`**

```python
"""The fetch ladder. ONE generic crawler over the sources.yml allowlist.

  robots.txt -> sitemap.xml -> structured feed -> HTML + LLM

Guardrails (spec §2) — these address COPYRIGHT/CONTRACT risk, a different axis from
bot-walls, and none of them has an override:

  * NEVER authenticate. No login, no account, no session cookie, no registration or
    NDA-gated page. This is the one bright line every scraping case that went badly
    crossed. Defeating a bot-detection WAF on a public no-login page is the protected
    case; crossing a login is not.
  * Identify honestly (UA), 1 req / 3-5s per domain, back off on 429/503, daily cap.
  * Conditional GETs (ETag / If-Modified-Since); nothing is refetched inside 24h.

On 8GB: exactly ONE long-lived stealth browser session per run. Never call the one-shot
StealthyFetcher.fetch() in a loop — it launches and kills a Chromium per call.
"""
import logging
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from . import extract, score
from .config import settings
from .db import get_conn, save_listing

log = logging.getLogger("openlease")

SOURCES: dict[str, list[dict]] = yaml.safe_load(
    (Path(__file__).parent / "data" / "sources.yml").read_text()
)
_ROBOTS: dict[str, urllib.robotparser.RobotFileParser] = {}
_LAST_HIT: dict[str, float] = {}


def _domain(url: str) -> str:
    return urlparse(url).netloc


# CORRECTION (live run): do NOT use RobotFileParser.read(). It calls urllib.request.urlopen,
# which sends `Python-urllib/3.11` — and broker WAFs 403 that UA on sight. RobotFileParser
# turns a 403 into `disallow_all = True`, so we silently self-blocked on sites whose
# robots.txt WELCOMES us: rexfordindustrial.com serves `Disallow:` (empty = allow all),
# avisonyoung.us has no Disallow at all, metro-manhattan.com disallows only /blog/ paths.
# Measured: 8 of 16 allowlisted sources came back "disallowed" — zeroing out LA and Chicago
# ENTIRELY. That is not obeying robots.txt; it is obeying a WAF's opinion of a User-Agent we
# should never have sent. Fetch robots.txt with our own honest UA, over our own stack.
# After the fix: 16/16 allowed, and a real Disallow is still obeyed.
def _get_robots_txt(robots_url: str) -> tuple[int, str]:
    import httpx
    r = httpx.get(robots_url, headers={"User-Agent": settings.crawl_user_agent},
                  timeout=20.0, follow_redirects=True)
    return r.status_code, r.text


def robots(url: str) -> urllib.robotparser.RobotFileParser:
    d = _domain(url)
    if d not in _ROBOTS:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{urlparse(url).scheme}://{d}/robots.txt"
        rp.set_url(robots_url)
        try:
            status, text = _get_robots_txt(robots_url)
            if status in (401, 403):
                rp.disallow_all = True          # a refusal addressed to US, by name
            elif 400 <= status < 500:
                rp.allow_all = True             # no robots.txt => nothing is forbidden
            elif status >= 500:
                rp.disallow_all = True          # the site is broken, not refusing us
            else:
                rp.parse(text.splitlines())
        except Exception as e:  # noqa: BLE001 — unreadable robots.txt = we do not crawl
            log.warning("robots.txt unreadable for %s (%s) — treating as disallow", d, e)
            rp.disallow_all = True
        _ROBOTS[d] = rp
    return _ROBOTS[d]


def allowed(url: str) -> bool:
    return robots(url).can_fetch(settings.crawl_user_agent, url)


def _delay_for(url: str, src: dict) -> float:
    """The site's own Crawl-delay wins if it is SLOWER than ours. It is never used to go
    faster than our floor."""
    site = robots(url).crawl_delay(settings.crawl_user_agent)
    ours = float(src.get("crawl_delay") or settings.crawl_delay_seconds)
    return max(ours, float(site or 0))


def _throttle(url: str, src: dict) -> None:
    d = _domain(url)
    wait = _delay_for(url, src) - (time.monotonic() - _LAST_HIT.get(d, 0.0))
    if wait > 0:
        time.sleep(wait)
    _LAST_HIT[d] = time.monotonic()


def _under_daily_cap(url: str) -> bool:
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM crawl_log WHERE domain = ? "
            "AND date(fetched_at) = date('now')", (_domain(url),)
        ).fetchone()["c"]
    return n < settings.crawl_daily_cap_per_domain


def _seen_recently(url: str) -> bool:
    """Conditional-GET stand-in: nothing is refetched inside the TTL."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM crawl_log WHERE url = ? AND fetched_at > datetime('now', '-24 hours') "
            "LIMIT 1", (url,)
        ).fetchone()
    return row is not None


def _log_fetch(url: str, status: int, etag: str | None = None, last_mod: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO crawl_log (domain, url, status, etag, last_mod) VALUES (?, ?, ?, ?, ?)",
            (_domain(url), url, status, etag, last_mod),
        )


def fetch(url: str, src: dict) -> str | None:
    """Default tier: curl_cffi impersonation, NO browser (handles ~95% of regional broker
    sites — they're server-rendered). Stealth tier only for the walled ones."""
    if not allowed(url):
        log.info("robots.txt disallows %s — skipping", url)
        return None
    if not _under_daily_cap(url):
        log.info("daily cap reached for %s — skipping", _domain(url))
        return None
    _throttle(url, src)

    if src.get("tier") == "stealth" and settings.crawl_stealth:
        return _stealth_fetch(url)

    from scrapling.fetchers import FetcherSession
    with FetcherSession(impersonate="chrome", stealthy_headers=True, retries=3) as s:
        page = s.get(url)
    _log_fetch(url, getattr(page, "status", 0))
    if getattr(page, "status", 0) in (429, 503):
        log.warning("%s -> %s, backing off", url, page.status)
        time.sleep(_delay_for(url, src) * 4)
        return None
    return page.body if getattr(page, "status", 0) == 200 else None


_STEALTH = None


def _stealth_fetch(url: str) -> str | None:
    """ONE long-lived session for the whole run. Never StealthyFetcher.fetch() in a loop —
    that launches and kills a Chromium per call and will bring an 8GB machine to its knees."""
    global _STEALTH
    try:
        from scrapling.fetchers import StealthySession
    except ImportError:
        log.warning("stealth tier unavailable — run `scrapling install` (~400-600MB "
                    "Chromium, one time). Skipping %s.", url)
        return None
    if _STEALTH is None:
        _STEALTH = StealthySession(headless=True, max_pages=2, disable_resources=True,
                                   solve_cloudflare=True)
        _STEALTH.__enter__()
    try:
        page = _STEALTH.fetch(url)
        _log_fetch(url, getattr(page, "status", 0))
        return page.body if getattr(page, "status", 0) == 200 else None
    except Exception as e:  # noqa: BLE001
        log.warning("stealth fetch failed for %s: %s", url, e)
        return None


def close() -> None:
    global _STEALTH
    if _STEALTH is not None:
        _STEALTH.__exit__(None, None, None)
        _STEALTH = None


def sitemap_urls(base: str, src: dict) -> list[str]:
    """Rung 2. <lastmod> is what drives a recrawl; absent one, the URL is crawled once."""
    import re
    body = fetch(urljoin(base, "/sitemap.xml"), src)
    if not body:
        return []
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)


def crawl_source(src: dict, metro: str, limit: int = 100) -> list[dict]:
    """Descend the ladder, stopping at the highest rung that produces listings."""
    out: list[dict] = []

    if src.get("rung") == "feed_wp" and src.get("feed"):
        body = fetch(src["feed"], src)
        if body:
            import json as _json
            try:
                items = _json.loads(body)
            except _json.JSONDecodeError:
                items = []
            for item in items[:limit]:
                d = extract.from_wp_json(item, src, metro)
                if d:
                    out.append(d)
            if out:
                return out                      # the rung worked — do not descend
            log.info("%s: wp-json returned nothing usable, descending the ladder", src["key"])

    urls = [u for u in sitemap_urls(src["url"], src) if "listing" in u or "propert" in u]
    urls = urls[:limit] or [src["url"]]

    for url in urls:
        if _seen_recently(url):
            continue
        body = fetch(url, src)
        if not body:
            continue
        d = extract.from_jsonld(body, url, src, metro)
        if not d and src.get("rung") != "jsonld":
            try:
                from scrapling.core.custom_types import TextHandler  # noqa: F401
                from scrapling.parser import Selector
                md = Selector(body).css_first("body").get_all_text(strip=True)
            except Exception:  # noqa: BLE001
                md = body
            d = extract.from_html_llm(md, url, src, metro)
        if d:
            out.append(d)
    return out


def run(metro: str | None = None, limit: int = 100) -> dict:
    """Crawl every allowlisted source (optionally one metro). Enriches each new listing
    with Walk/Transit score at ingest — the ONLY time Overpass is ever called."""
    metros = [metro] if metro else list(SOURCES)
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": []}
    try:
        for m in metros:
            for src in SOURCES.get(m, []):
                try:
                    recs = crawl_source(src, m, limit)
                except Exception as e:  # noqa: BLE001 — one bad source must not kill the run
                    log.warning("source %s failed: %s: %s", src["key"], type(e).__name__, e)
                    stats["errors"].append(f"{src['key']}: {type(e).__name__}")
                    continue
                stats["fetched"] += len(recs)
                for rec in recs:
                    if not rec.get("lat"):
                        stats["skipped"] += 1   # no point = no scoring; still stored
                    lid = save_listing(rec)
                    stats["saved"] += 1
                    if rec.get("lat"):
                        try:
                            score.enrich(lid)
                        except Exception as e:  # noqa: BLE001
                            log.warning("scoring failed for %s: %s", lid, e)
    finally:
        close()                                 # always release the Chromium
    return stats
```

> **Correction (Task 10, verified against the installed `scrapling==0.4.10` package):**
> two real defects found implementing this against the pinned version, plus one scope gap
> worth flagging rather than silently papering over:
>
> 1. **`Selector.css_first()` does not exist in 0.4.10.** `Selector(body).css_first("body")`
>    raises `AttributeError: 'Selector' object has no attribute 'css_first'`. In this
>    version `.css(selector)` returns a `Selectors` list; the first match is its `.first`
>    property (`Optional[Selector]`). `app/crawl.py` replaces the line above with a small
>    `_to_markdown()` helper: `Selector(body).css("body").first` (falling back to the raw
>    `body` string if `.first` is `None`), then `.get_all_text(strip=True)` as before.
>    (`scrapling.core.shell.Convertor` — the class the intro prose alludes to — exists but
>    requires the `markdownify` package, which is **not** a dependency of the `fetchers`
>    extra and is not installed; it also targets writing a file for the `scrapling extract`
>    CLI command, not library reuse. The plan's own manual `get_all_text()` approach, once
>    `css_first` is fixed to `.css().first`, is the correct choice — no new dependency.)
> 2. **`import httpx` (crawl.py) and `from fastapi import BackgroundTasks` (routes_crawl.py)
>    are unused** in the code as written — `fetch()` calls `scrapling`'s `FetcherSession`,
>    never `httpx` directly, and `/api/crawl` returns synchronously rather than
>    backgrounding. Both dropped.
> 3. **Scope gap, not fixed:** none of the three extraction rungs (`from_wp_json`,
>    `from_jsonld`, `from_html_llm`) resolves a crawled listing's address to `lat`/`lng` —
>    T10's own "Consumes" list names `db.save_listing`, `ai`, `score.enrich`, `settings`,
>    but never `registry.geocoder`. The `run()` code above (correctly) only calls
>    `score.enrich(lid)` `if rec.get("lat")`, but since no rung ever sets it, **every
>    crawled listing is stored geocode-less and never scored** until a future task adds a
>    geocode-the-address step (via `registry.geocoder(metro)`) before `save_listing`. Left
>    as a documented gap rather than an unrequested, unverified geocoding integration — see
>    `task-10-report.md` for the reasoning.
>
> All fixes are in `app/crawl.py`; see `task-10-report.md` for the raw before/after
> evidence (`css_first` reproduced live against the installed package).

> **Correction (Task 10 fix pass — review findings, six more defects in the code above):**
>
> 1. **`fetch()`/`_stealth_fetch()` return `bytes`, type-hinted (and behaving) as `str` —
>    the crawler broke on first real contact.** scrapling's `Response.body`
>    (`engines/toolbelt/custom.py`) is a `@property` returning `self._raw_body`, which is
>    **bytes** for both fetch tiers — verified against the installed 0.4.10 package.
>    `sitemap_urls()`'s `<loc>` regex and `extract.from_jsonld()`'s script regex are both
>    `str`-only, so this raised `TypeError: cannot use a string pattern on a bytes-like
>    object` on the very first successful fetch, for 14 of the 16 configured sources (only
>    `feed_wp` sources, which parse the body as JSON rather than regexing it, were
>    unaffected). Fixed with a `_decode()` helper: decodes using the response's own
>    detected charset (`Response.encoding`, read from the Content-Type header) where
>    scrapling exposes it, falling back to utf-8 with `errors="replace"` so one bad byte on
>    a broker page can't kill the crawl. Both `fetch()` and `_stealth_fetch()` return
>    `_decode(page)` instead of `page.body`.
> 2. **The default tier's UA never matched what `robots()` checked permissions under.**
>    `robots()`/`allowed()` evaluate `settings.crawl_user_agent` (OpenLeaseBot); the actual
>    fetch used `FetcherSession(impersonate="chrome", ...)` with no `headers=` override, so
>    curl_cffi auto-generated a **Chrome** User-Agent — checking robots.txt as one identity
>    and then presenting as another on the wire. Fixed: the default tier now passes
>    `headers={"User-Agent": settings.crawl_user_agent, ...}` explicitly. Verified live
>    against the installed curl_cffi that an explicit `headers=` User-Agent wins over
>    `impersonate`'s auto-generated one (the TLS/JA3 fingerprint and `Sec-Ch-Ua` client
>    hints — the part that actually helps against a bot-wall — stay impersonated; only the
>    UA header changes). The **stealth tier is deliberately different and untouched**: it
>    exists specifically to defeat a bot-detection WAF on a public page, so full Chrome
>    impersonation there is the point, not a bug — see the comment in `_stealth_fetch()`.
> 3. **Stealth graceful-degradation caught the wrong exception.** `_stealth_fetch()` only
>    caught `ImportError` around the scrapling import — but `playwright`/`patchright` are
>    ordinary pip deps (requirements.txt) that always import fine. The REAL failure when
>    `scrapling install` hasn't downloaded Chromium is raised from inside
>    `StealthySession.start()`, via the previously-unguarded `_STEALTH.__enter__()` —
>    verified LIVE against the installed package with Chromium genuinely absent in this
>    environment:
>    ```
>    patchright._impl._errors.Error: BrowserType.launch_persistent_context: Executable
>    doesn't exist at .../chrome-mac-arm64/Google Chrome for Testing.app/.../Google Chrome
>    for Testing
>    ...Please run the following command to download new browsers:
>        playwright install
>    ```
>    Note it's **`patchright`'s** error class, not `playwright`'s as this section originally
>    guessed — `StealthySession` launches its browser through `patchright.sync_api`, not
>    vanilla playwright (only imported for type hints). Fixed by wrapping
>    `candidate.__enter__()` in its own `try`/`except Exception`, logging the actionable
>    `scrapling install` message, and returning `None` (never reusing a half-initialized
>    session on the next call — `_STEALTH` is only assigned after a successful `__enter__`).
> 4. **The default tier's 429/503 backoff was a flat `* 4` multiplier, and the stealth tier
>    had no backoff at all.** The constraint requires **exponential** backoff; a flat
>    multiplier never compounds against a domain that keeps 429ing, and `_stealth_fetch()`
>    (where `ksr` — sources.yml's own "429-throttles aggressively" note — actually lives,
>    `tier: stealth`) silently returned `None` with no wait at all. Fixed with a shared
>    `_backoff(url, src, status)`: `base_delay * 2**streak`, streak incrementing per
>    CONSECUTIVE 429/503 for that domain (capped at 6, ~64x) and resetting to 0 on any other
>    status. Both tiers call it.
> 5. **Conditional GETs were never implemented — `crawl_log.etag`/`last_mod` were dead
>    columns.** They were declared in the schema and accepted as `_log_fetch()` parameters,
>    but always called with `None`; no `If-None-Match`/`If-Modified-Since` header was ever
>    sent. Implemented for real: `_conditional_headers(url)` reads the most recent
>    etag/last_mod captured for that exact URL and sends it back as
>    `If-None-Match`/`If-Modified-Since` on the next fetch (both tiers — the stealth tier
>    via `StealthySession.fetch(url, extra_headers=...)`); a `304` response is treated as
>    "nothing to extract" (returns `None`, same as any other non-200), not a failure. The
>    response's own `etag`/`last-modified` headers are captured and persisted via
>    `_log_fetch()` on every fetch, so the columns are no longer always-NULL.
> 6. **Nothing geocoded — closing the Step 4/Item 3 gap directly above.** That correction
>    left crawl-time geocoding as a documented scope gap. The reviewer who found it also
>    found it belongs to Task 10 (no other task claims it, and the product's definition of
>    done requires a real crawl to produce listings with a map pin and a Walk Score) — so
>    **Task 10 now owns crawl-time geocoding**, closing that gap rather than leaving it
>    open. `crawl._geocode(address, metro)` dispatches to the metro's existing free,
>    keyless provider (NYC: `providers.geosearch.geocode()`, already returns lat/lng
>    directly; the other three: `registry.parcel_provider(metro).geocode()`, a NEW function
>    on each of `parcel_miami.py`/`parcel_la.py`/`parcel_chicago.py` — same free
>    ArcGIS/Socrata endpoints those modules already query for `lookup()`, just asking for
>    point geometry instead of attributes-only; see `task-10-report.md` for the per-metro
>    geometry-shape details (point vs. polygon, `returnCentroid` support) verified live
>    while adding these). No new geocoding dependency,
>    no new API key. `crawl._maybe_geocode(d)` is called on every extracted record, in
>    `crawl_source()`, right after extraction; a failure (no match, mirror down, malformed
>    response) leaves `lat`/`lng` **absent** — never a fabricated `(0, 0)` (constraints.md:
>    `None != 0 != "lookup failed"`, and `0,0` is the Gulf of Guinea) — and logs loudly.
>    `run()`'s existing `score.enrich(lid)` call (already gated on `rec.get("lat")`) now
>    actually fires for real crawled listings; it's now paced with
>    `time.sleep(settings.overpass_pace_seconds)` between listings (new setting, default
>    2.0s) so a real crawl doesn't hammer the one shared free Overpass mirror the way
>    Task 8 did doing 12 listings back-to-back — an Overpass failure there was already (and
>    remains) a logged skip via `score.enrich`'s existing `except Exception` in `run()`,
>    never a crash and never a fake `0`.
>
> All six fixes are in `app/crawl.py` (fix 6's provider-side half is in the three
> `parcel_*.py` files); see `task-10-report.md` for the raw RED/GREEN evidence, including
> the `patchright` exception reproduced live.

- [ ] **Step 5: Write `routes_crawl.py`**

```python
"""POST /api/crawl — run the ladder over sources.yml. Admin-only (auth-gated), and
long-running, so it reports what it did rather than streaming."""
from fastapi import BackgroundTasks, Depends

from . import crawl
from .app import app, require_auth


@app.post("/api/crawl")
def api_crawl(metro: str | None = None, limit: int = 100, _=Depends(require_auth)):
    return crawl.run(metro, limit)


@app.get("/api/crawl/sources")
def api_sources(_=Depends(require_auth)):
    """What we are allowed to fetch, and on which rung. The allowlist IS the scope."""
    return crawl.SOURCES
```

Add to `app.py`'s route-import block:

```python
from . import routes_crawl      # noqa: E402,F401  (T10)
```

- [ ] **Step 6: Capture the two feed fixtures**

```bash

curl -s -A "OpenLeaseBot/0.1" \
  'https://www.ripcony.com/wp-json/wp/v2/property-listings?per_page=3' \
  -o tests/fixtures/ripco_wpjson.json
.venv/bin/python -c "
import json; d=json.load(open('tests/fixtures/ripco_wpjson.json'))
print(len(d), 'items; keys:', sorted(d[0])[:12])"
```

Expected: 3 items. If the field names differ from what `from_wp_json` picks, adjust the
`pick(...)` key lists — **not** by adding a RIPCO-specific parser, but by widening the
generic key list.

For the JSON-LD fixture, save any single detail page from an allowlisted `rung: jsonld`
source that carries a `<script type="application/ld+json">` block:

```bash
.venv/bin/python -c "
from app import crawl
from app.crawl import SOURCES
src = [s for s in SOURCES['la'] if s['key']=='rexford'][0]
urls = [u for u in crawl.sitemap_urls(src['url'], src) if 'propert' in u][:1]
print('detail:', urls)
body = crawl.fetch(urls[0], src) if urls else None
open('tests/fixtures/jsonld_listing.html','w').write(body or '')
print('bytes:', len(body or ''))
"
```

If that site's robots.txt disallows the path, the fetch returns `None` and the file is
empty — **that is the correct outcome**, and the JSON-LD test then runs against a
hand-written minimal fixture instead. Write one:

```bash
cat > tests/fixtures/jsonld_listing.html <<'HTML'
<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Place",
 "name":"Ground floor retail",
 "address":{"@type":"PostalAddress","streetAddress":"1550 N Damen Ave","addressLocality":"Wicker Park"},
 "floorSize":{"@type":"QuantitativeValue","value":"2100","unitCode":"FTK"},
 "offers":{"@type":"Offer","price":"58","priceCurrency":"USD"},
 "image":"https://cdn.example.com/photo1.jpg"}
</script></head><body>
<p>Marketing copy we must never persist: "An UNRIVALED opportunity!!"</p>
</body></html>
HTML
```

> **Correction (Task 10, explicit scope override from the task owner):** neither live
> capture above was run. The task instructions for this pass were explicit: *"Do NOT run
> a live crawl in this task. I will run the real crawl separately, after review."* Hitting
> `ripcony.com`'s wp-json endpoint or fetching a `rexford` detail page are both real
> network calls against a real broker site, so both were replaced with hand-built
> fixtures instead of live captures:
>
> - `tests/fixtures/jsonld_listing.html` is the hand-written fallback given above,
>   verbatim (the brief already anticipates this path for the case where robots.txt
>   disallows the detail page).
> - `tests/fixtures/ripco_wpjson.json` has no hand-written fallback in the brief (only a
>   live `curl`), so a synthetic 3-item WP-REST fixture was constructed by hand instead —
>   modeled on real WordPress `wp/v2/{cpt}` response shape (`title.rendered`, `link`,
>   `guid.rendered`) with three different field-name conventions across the three items
>   (`acf.address`, `meta.street_address`, `acf.property_address`) specifically to
>   exercise `from_wp_json`'s `pick()` fallback chain, not just its happy path. This is
>   not a substitute for verifying the real endpoint's actual field names — that
>   verification still needs a real, reviewed crawl run, which is exactly what this task
>   was told not to do.

- [ ] **Step 7: Write the test**

`tests/test_extract.py`:

```python
"""The two keyless fast paths (wp-json, JSON-LD), and the two copyright invariants: we
never persist the page's prose, and we never download a photo."""
import json
import os
import pathlib

os.environ["ANTHROPIC_API_KEY"] = ""      # keyless: the LLM rung must be skipped, loudly

import pytest  # noqa: E402

from app import extract  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
SRC = {"key": "test", "name": "Test Brokerage", "url": "https://example.com"}


def test_wp_json_fast_path():
    p = FIX / "ripco_wpjson.json"
    if not p.exists():
        pytest.skip("ripco_wpjson.json not captured — see Task 10 Step 6")
    items = json.loads(p.read_text())
    got = [extract.from_wp_json(i, SRC, "nyc") for i in items]
    got = [g for g in got if g]
    assert got, "the wp-json rung produced nothing — check the pick() key lists"
    d = got[0]
    assert d["source"] == "test" and d["metro"] == "nyc"
    assert d["source_url"].startswith("http")
    assert d["address"]
    assert d["transaction_type"] == "lease"
    assert d["our_description"]                 # our sentence, not the post's content


def test_jsonld_fast_path():
    html = (FIX / "jsonld_listing.html").read_text()
    d = extract.from_jsonld(html, "https://example.com/l/1", SRC, "chi")
    assert d is not None
    assert d["address"] == "1550 N Damen Ave, Wicker Park"
    assert d["size_sf"] == 2100 and d["asking_rent"] == 58.0
    assert d["rent_unit"] == "sf_yr"
    assert json.loads(d["photo_urls_json"]) == ["https://cdn.example.com/photo1.jpg"]


def test_broker_prose_is_never_persisted():
    html = (FIX / "jsonld_listing.html").read_text()
    d = extract.from_jsonld(html, "https://example.com/l/1", SRC, "chi")
    blob = json.dumps(d)
    assert "UNRIVALED" not in blob, "the page's marketing copy leaked into a stored field"
    assert "description" not in d          # only `our_description` exists
    assert "Wicker Park" in d["our_description"] and "2,100 SF" in d["our_description"]


def test_photos_are_referenced_never_downloaded():
    """`photo_urls_json` holds the BROKER'S url. Nothing in extract.py fetches image
    bytes — if this ever changes, it is the CoStar v. CREXi fact pattern verbatim."""
    src = pathlib.Path(extract.__file__).read_text()
    for red_flag in ("httpx.get(photo", "download_image", "s3", "boto3", ".write(img"):
        assert red_flag not in src, f"extract.py appears to fetch/store image bytes: {red_flag}"


def test_llm_rung_is_skipped_loudly_without_a_key(caplog):
    out = extract.from_html_llm("# some page", "https://example.com/x", SRC, "nyc")
    assert out is None
    assert any("ANTHROPIC_API_KEY" in r.message for r in caplog.records), \
        "the LLM rung degraded SILENTLY — that is the failure mode this rule exists to stop"


def test_extract_schema_is_all_required_and_non_nullable():
    for name, f in extract.ListingExtract.model_fields.items():
        assert f.is_required(), f"{name} has a default -> optional param -> request HANGS"
        assert "NoneType" not in str(f.annotation), f"{name} is nullable -> union-param 400"
```

- [ ] **Step 8: Run — expect failure, then green**

```bash
.venv/bin/python -m pytest tests/test_extract.py -v
```

Expected: `6 passed` (or 5 + 1 skipped if the RIPCO capture is unavailable).

- [ ] **Step 9: Prove robots.txt is actually obeyed**

```bash
.venv/bin/python -c "
from app import crawl
print('ripco /wp-json  ->', crawl.allowed('https://www.ripcony.com/wp-json/wp/v2/property-listings'))
print('a disallowed path ->', crawl.allowed('https://www.ripcony.com/wp-admin/'))
print('crawl delay      ->', crawl._delay_for('https://www.ripcony.com/', {'key':'x'}), 'seconds')
"
```

Expected: `True`, `False`, and a delay of at least 4.0s. If the second line prints `True`,
`allowed()` is not reading robots — stop and fix it before crawling anything.

- [ ] **Step 10: Do one real, small crawl**

```bash
.venv/bin/python -c "
import logging; logging.basicConfig(level=logging.INFO)
from app import crawl, db
db.init_db()
print(crawl.run('nyc', limit=5))
with db.get_conn() as c:
    for r in c.execute(\"SELECT source, address, size_sf, our_description FROM listing WHERE source!='seed' LIMIT 5\"):
        print(dict(r))
"
```

Expected: a handful of real RIPCO listings, each with an `our_description` we wrote, a
`source_url` pointing at the broker's page, and no `description` column anywhere.

> **Correction (Task 10, explicit scope override):** Steps 9 and 10 were NOT run in this
> pass, for the same reason as the Step 6 correction above — both hit `www.ripcony.com`
> over the real network (`allowed()`/`_delay_for()` fetch and cache its real
> `robots.txt`; `run()` fetches its real wp-json feed and writes rows to the DB), and the
> task instructions were explicit that a live crawl happens separately, after review.
> `allowed()`'s robots-obeying logic (and the crawl-delay floor, the daily cap, the
> recrawl dedup) are instead proven HERMETICALLY in `tests/test_crawl.py`, by seeding
> `crawl._ROBOTS` with a `RobotFileParser` built from canned lines (`.parse()`, never
> `.read()` — no network) for a fake domain, then asserting `allowed()`/`_delay_for()`
> read it correctly. This is a real, if partial, substitute for Step 9 — it proves the
> mechanism works, but it is not the same as proving `ripcony.com`'s ACTUAL live
> robots.txt permits what `sources.yml` assumes; that still needs the reviewed live run
> the task owner will do separately.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat(openlease): the fetch ladder — robots->sitemap->feed->LLM, one crawler, no per-site parsers"
```

---

### Task 11: Government supply + CSV import

Zero ToS surface, and a lead source no scraper gives you: NYC publishes a **vacancy flag**
on every storefront in the city.

**CORRECTION (verified live 2026-07-12, at implementation time) — both dataset IDs are
real and keyless, but the field names/join key below had drifted, the same failure mode
Task 9 found on all four metros' parcel data:**

1. **`92iy-9c3n`** ("Storefronts Reported Vacant or Not") has **no**
   `primary_business_address`, `street_number`, or `street_name` column. The real address
   column is `property_street_address_or` (Socrata's own truncation of "Property Street
   Address or Storefront Address" — it's the pre-joined full address, e.g. "271 BROAD
   STREET"), with `property_number` + `property_street` as a fallback. Run the plan's
   original code against the real response and every row's `addr` came back `""` — the
   row-skip guard (`if not addr or not bbl: continue`) then silently dropped **all** of
   them. Zero storefronts, not five. Also: `borough` comes back ALL CAPS
   ("STATEN ISLAND"); the rest of the app (`metros.yml`, the hard borough filter) uses
   Title Case ("Staten Island") — stored verbatim it can never match a `boroughs` filter,
   so the provider now `.title()`s it.
2. **`bnx9-e6tj`** ("ACRIS - Real Property Master") has **no** `borough`/`block`/`lot`
   columns at all. Querying it that way returns **HTTP 400** ("Unrecognized arguments
   [block, borough, lot]") — not an empty list, a hard failure. ACRIS is split across
   datasets: **`8h5j-fqxa`** ("ACRIS - Real Property Legals") holds the
   borough/block/lot → `document_id` join; `bnx9-e6tj` holds
   `document_id` → `doc_type`/`document_amt`/`recorded_datetime`. A BBL-to-signal lookup
   needs **both calls, in sequence** (confirmed live end-to-end against the Empire State
   Building's BBL, 1008350041 — 5 Legals rows joined to 5 Master rows with real doc types
   AGMT/RPTT/ASST/MLEA and a real $49,739,616.16 RPTT transfer).

The code blocks below are corrected to match. See `app/providers/gov_nyc.py` for the
final, committed version and `tests/test_import.py` for the fixture-backed regression
tests that lock this in (built from real captured responses, not hand-written stubs).

**Files:**
- Create: `app/providers/gov_nyc.py`, `app/routes_import.py`
- Modify: `app/app.py` (route import)
- Test: `tests/test_import.py` (a dedicated file, not an extension of `tests/test_smoke.py`
  as originally planned — keeps this task's fixture-heavy provider tests out of the
  shared smoke suite other tasks also touch)

**Interfaces:**
- Consumes: `db.save_listing` (T2), `cache.cached` (T1).
- Produces:
  - `gov_nyc.storefronts(limit: int = 1000, vacant_only: bool = True) -> list[dict]` — normalized Listing dicts
  - `gov_nyc.acris_signals(bbl: str) -> list[dict]` — `[{doc_type, amount, date}]`, newest first
  - `import_csv(rows: Iterable[dict], metro: str) -> int` (in `routes_import.py`)
  - `POST /api/import/storefronts`, `POST /api/import/csv`

- [ ] **Step 1: Write `providers/gov_nyc.py`**

```python
"""Free NYC government supply — zero ToS surface, and one thing no broker feed has: a
VACANCY FLAG on every ground- and second-floor commercial space in the city.

  Storefront Registry (Socrata 92iy-9c3n, keyless) — address, BBL, lat/lng, business
    activity, and `vacant_on_12_31`. A vacancy is a lead.
  ACRIS (keyless) — deeds and mortgages with amounts and dates. A big mortgage recorded
    against a building with a vacant storefront is a distress signal.

Verified live 2026-07-12 — the plan's field names/join key DRIFTED on both endpoints (the
exact failure Task 9 found on all four metros' parcel data):

  1. `92iy-9c3n` has no `primary_business_address`, `street_number`, or `street_name`
     column. The real columns are `property_street_address_or` (the pre-joined full
     address, e.g. "271 BROAD STREET") and, as a fallback, `property_number` +
     `property_street`. Run against the field names the plan guessed, every real row's
     address came back "" and was silently dropped — zero storefronts, not five.
  2. `bnx9-e6tj` ("ACRIS - Real Property Master") has NO borough/block/lot columns at
     all — querying it that way is a 400 ("Unrecognized arguments"), not an empty list.
     ACRIS is split across datasets: `8h5j-fqxa` ("ACRIS - Real Property Legals") holds
     the borough/block/lot -> document_id join; `bnx9-e6tj` holds
     document_id -> doc_type/amount/date. A BBL-to-signal lookup needs both, in sequence.
"""
import httpx

from ..cache import cached

STOREFRONT = "https://data.cityofnewyork.us/resource/92iy-9c3n.json"
ACRIS_LEGALS = "https://data.cityofnewyork.us/resource/8h5j-fqxa.json"   # bbl -> document_id
ACRIS = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"          # document_id -> doc


def storefronts(limit: int = 1000, vacant_only: bool = True) -> list[dict]:
    """Vacant storefronts as Listing dicts. `source_url` points at the city's own record
    for the BBL, so the row is traceable and we invent nothing."""
    where = "vacant_on_12_31='YES'" if vacant_only else "1=1"

    def fetch():
        r = httpx.get(STOREFRONT, params={"$where": where, "$limit": limit}, timeout=60.0)
        r.raise_for_status()
        return r.json()

    rows = cached("nyc_storefront", "query", {"where": where, "limit": limit}, fetch)
    out = []
    for r in rows:
        bbl = r.get("bbl")
        addr = r.get("property_street_address_or") or (
            f"{r.get('property_number', '')} {r.get('property_street', '')}".strip())
        lat, lng = r.get("latitude"), r.get("longitude")
        if not addr or not bbl:
            continue
        out.append({
            "source": "nyc_storefront",
            "source_url": f"https://data.cityofnewyork.us/resource/92iy-9c3n.json?bbl={bbl}",
            "metro": "nyc",
            "status": "available",
            "address": addr,
            # the dataset's own borough names come back ALL CAPS ("STATEN ISLAND"); the
            # rest of the app (metros.yml, the hard borough filter) uses Title Case
            # ("Staten Island") — normalize here or a borough filter can never match.
            "borough": (r.get("borough") or "").title() or None,
            "lat": float(lat) if lat else None,
            "lng": float(lng) if lng else None,
            "property_type": "retail",
            "transaction_type": "lease",
            "parcel_id": f"nyc:{bbl}",
            "our_description": (
                f"Vacant ground-floor commercial space at {addr}, from the City of New York's "
                f"Storefront Registry (last reported use: "
                f"{r.get('primary_business_activity') or 'not stated'}). No broker is attached — "
                f"this is a vacancy lead, not a listing."
            ),
        })
    return out


def acris_signals(bbl: str) -> list[dict]:
    """Deeds/mortgages recorded against a BBL. A large recent mortgage under a vacant
    storefront is the distress signal worth a call.

    Two Socrata calls, not one: `8h5j-fqxa` (Legals) maps borough/block/lot -> the
    document_ids recorded against that lot; `bnx9-e6tj` (Master) maps those document_ids
    to doc_type/amount/date. Master alone has no BBL column to query by."""
    if not bbl or len(bbl) < 10:
        return []
    borough, block, lot = bbl[0], int(bbl[1:6]), int(bbl[6:10])

    def fetch_legals():
        r = httpx.get(ACRIS_LEGALS, params={
            "borough": borough, "block": block, "lot": lot, "$limit": 200}, timeout=60.0)
        r.raise_for_status()
        return r.json()

    legals = cached("acris_legals", "bbl", {"bbl": bbl}, fetch_legals)
    doc_ids = sorted({r["document_id"] for r in legals if r.get("document_id")})
    if not doc_ids:
        return []   # legitimate -- not every parcel has ACRIS history; never fire Master

    def fetch_master():
        where = "document_id in(" + ",".join(f"'{d}'" for d in doc_ids) + ")"
        r = httpx.get(ACRIS, params={
            "$where": where, "$order": "recorded_datetime DESC", "$limit": 20}, timeout=60.0)
        r.raise_for_status()
        return r.json()

    rows = cached("acris_master", "doc_ids", {"doc_ids": doc_ids}, fetch_master)
    return [{"doc_type": r.get("doc_type"), "amount": r.get("document_amt"),
             "date": r.get("recorded_datetime")} for r in rows]
```

- [ ] **Step 2: Write `routes_import.py`**

```python
"""Bring-your-own supply: the city's vacancy feed, and your own CSV (a broker export, a
CoStar pull — whatever you already licensed). Neither touches a broker site."""
import csv
import io

from fastapi import Depends, UploadFile

from . import db, score
from .app import app, require_auth
from .models import METRO_KEYS
from .providers import gov_nyc

# CSV column -> Listing field. Anything else in the file is ignored.
CSV_MAP = {
    "address": "address", "neighborhood": "neighborhood", "borough": "borough",
    "type": "property_type", "property_type": "property_type",
    "size": "size_sf", "size_sf": "size_sf", "sf": "size_sf",
    "rent": "asking_rent", "asking_rent": "asking_rent",
    "rent_unit": "rent_unit", "lease_type": "lease_type",
    "price": "sale_price", "sale_price": "sale_price",
    "lat": "lat", "lng": "lng", "broker": "broker_name", "broker_firm": "broker_firm",
    "phone": "broker_phone", "email": "broker_email", "url": "source_url",
    "description": "our_description",   # YOUR file, YOUR words — you own this one
}
_NUM = {"size_sf", "sale_price"}
_FLOAT = {"asking_rent", "lat", "lng"}


@app.post("/api/import/storefronts")
def import_storefronts(limit: int = 500, _=Depends(require_auth)):
    """NYC only — it is the only one of the four metros that publishes a vacancy feed."""
    recs = gov_nyc.storefronts(limit=limit)
    saved = 0
    for rec in recs:
        lid = db.save_listing(rec)
        saved += 1
        if rec.get("lat"):
            try:
                score.enrich(lid)
            except Exception:  # noqa: BLE001 — a scoring failure must not lose the lead
                pass
    return {"fetched": len(recs), "saved": saved}


@app.post("/api/import/csv")
async def import_csv_route(file: UploadFile, metro: str = "nyc", _=Depends(require_auth)):
    if metro not in METRO_KEYS:
        return {"error": f"metro must be one of {METRO_KEYS}"}
    text = (await file.read()).decode("utf-8-sig")
    return {"saved": import_csv(csv.DictReader(io.StringIO(text)), metro)}


def import_csv(rows, metro: str) -> int:
    saved = 0
    for i, row in enumerate(rows):
        rec: dict = {"metro": metro, "source": "csv"}
        for col, val in row.items():
            field = CSV_MAP.get((col or "").strip().lower())
            if not field or val in (None, ""):
                continue
            try:
                if field in _NUM:
                    rec[field] = int(float(str(val).replace(",", "").replace("$", "")))
                elif field in _FLOAT:
                    rec[field] = float(str(val).replace(",", "").replace("$", ""))
                else:
                    rec[field] = val
            except ValueError:
                continue
        if not rec.get("address"):
            continue
        rec.setdefault("source_url", f"csv://{metro}/{i}/{rec['address']}")
        db.save_listing(rec)
        saved += 1
    return saved
```

Add to `app.py`'s route-import block:

```python
from . import routes_import     # noqa: E402,F401  (T11)
```

- [ ] **Step 3: Write `tests/test_import.py`** (a dedicated file, not an extension of
  `tests/test_smoke.py`)

Built around real captured Socrata responses (`tests/fixtures/gov_nyc_storefronts.json`,
`gov_nyc_acris_legals.json`, `gov_nyc_acris_master.json` — pulled once by hand, see Step 4),
so the field-name/join-key regressions above are locked in by fixture data, not a
hand-written stub that would have happily "passed" against the plan's wrong field names.
Covers: `storefronts()` normalizing the real response (including the borough Title-Case
fix and the address fallback), rows with a missing address/bbl being dropped rather than
faked, the `vacant_only` `$where` toggle, `acris_signals()`'s two-step Legals->Master join
(and that an empty Legals result short-circuits before ever querying Master), `POST
/api/import/storefronts` saving leads with no invented size/rent/broker and auth-gating,
and `import_csv`/`POST /api/import/csv`'s round trip, unmappable-file rejection,
unmapped-column tolerance, `$`/comma-formatted numeric parsing, and unknown-metro handling.
The CSV round-trip test itself is unchanged from the plan's original:

```python
def test_csv_import_round_trip(isolated_db):
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        csv_bytes = (
            b"address,type,size,rent,lat,lng,description\n"
            b"123 Test Ave,retail,1800,72,40.75,-73.98,Corner unit from my own export\n"
        )
        r = c.post("/api/import/csv?metro=nyc",
                   files={"file": ("my_export.csv", io.BytesIO(csv_bytes), "text/csv")})
        assert r.status_code == 200 and r.json()["saved"] == 1, r.text
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM listing WHERE address='123 Test Ave'").fetchone()
        assert row["size_sf"] == 1800 and row["asking_rent"] == 72.0
        assert row["source"] == "csv"
```

- [ ] **Step 4: Run, then pull a real vacancy list**

```bash
.venv/bin/python -m pytest tests/test_import.py -v
.venv/bin/python -c "
from app.providers import gov_nyc
s = gov_nyc.storefronts(limit=5)
print(len(s), 'vacant storefronts')
for x in s[:3]: print(' ', x['address'], '|', x['parcel_id'])
sig = gov_nyc.acris_signals(s[0]['parcel_id'].split(':')[1]) if s else []
print('acris:', sig[:2])
"
```

**Actual output, live, 2026-07-12** (against the corrected code — the plan's original
field names, run unmodified, return zero storefronts, not five):

```
5 vacant storefronts
  271 BROAD STREET | nyc:5005430010
  1366 CLOVE ROAD | nyc:5006550014
  693 HENDERSON AVENUE | nyc:5001730034
acris: []
```

An empty ACRIS list for the first result is legitimate — not every parcel has a recent
filing (confirmed separately: the Empire State Building's BBL, which does have ACRIS
history, round-trips through both calls correctly — see `tests/test_import.py`).
A citywide count check (`$select=count(*)&$where=vacant_on_12_31='YES'`) returned
**43,978** vacant storefronts citywide for the current reporting year — confirming this
is a real, substantial lead source, not a handful of rows.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(openlease): free government supply — NYC storefront vacancies + ACRIS, and CSV import"
```

---

### Task 12: Semantic ranking — Voyage embeddings + cosine, fused with RRF

The optional key. **Nothing outside `rank.py` and `registry.py` changes** — that is the
payoff of RRF: one more list in the same call.

**Files:**
- Create: `app/providers/voyage.py`
- Modify: `app/rank.py` (append the cosine list to the fusion), `app/db.py` (append `listing_vec`)
- Test: extend `tests/test_rank.py`

**Interfaces:**
- Consumes: `registry.embedder()` (T7), `rank.rrf` (T3).
- Produces:
  - `voyage.VoyageEmbedder.embed(texts: list[str], input_type: str) -> list[list[float]]` — L2-normalized
  - `rank.embed_listings(listing_ids: list[int]) -> int` — backfills `listing_vec`, returns the count
  - `rank.cosine_ids(candidate_ids: list[int], query_text: str) -> list[int]`
  - `db.save_vector(listing_id: int, vec: list[float]) -> None`, `db.load_vectors(ids: list[int]) -> tuple[list[int], np.ndarray]`

- [ ] **Step 1: Append `listing_vec` to `db.SCHEMA` and add vector persistence**

```sql
-- float32 BLOBs, L2-normalized at write. Present only with a VOYAGE_API_KEY.
-- Deliberately NOT sqlite-vec: that needs enable_load_extension, which is ABSENT on stock
-- python.org macOS / pyenv / system python. It would work in Docker and break on the
-- user's Mac — the worst failure mode there is — and buys nothing at 5k rows, where a
-- brute-force numpy matmul is 0.84ms.
CREATE TABLE IF NOT EXISTS listing_vec (
    listing_id INTEGER PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    embedding  BLOB NOT NULL
);
```

```python
def save_vector(listing_id: int, vec) -> None:
    import numpy as np
    arr = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if n:
        arr = arr / n                      # L2-normalize at WRITE, so search is a dot product
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO listing_vec (listing_id, embedding) VALUES (?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET embedding = excluded.embedding",
            (listing_id, arr.tobytes()),
        )


def load_vectors(ids: list[int]):
    """-> (ids_present, M) where M is (n, dim) float32, row i = ids_present[i]."""
    import numpy as np
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    holes = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT listing_id, embedding FROM listing_vec WHERE listing_id IN ({holes})",
            ids,
        ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    got = [r["listing_id"] for r in rows]
    M = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    return got, M
```

- [ ] **Step 2: Write `providers/voyage.py`**

```python
"""Voyage embeddings — `voyage-4-lite`, 1024-dim. The free tier is 200M tokens; a
5,000-listing corpus is ~0.5M, so this is free forever in practice. Every call goes
through cache.cached(), so a listing is embedded once."""
import httpx

from ..cache import cached
from ..config import settings

URL = "https://api.voyageai.com/v1/embeddings"
MODEL = "voyage-4-lite"
DIM = 1024


class VoyageEmbedder:
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """input_type is 'document' at ingest and 'query' at search — Voyage encodes them
        differently, and mixing them measurably degrades retrieval."""
        def fetch():
            r = httpx.post(
                URL,
                headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
                json={"input": texts, "model": MODEL, "input_type": input_type},
                timeout=60.0,
            )
            r.raise_for_status()
            return r.json()

        data = cached("voyage", input_type, {"texts": texts, "model": MODEL}, fetch)
        return [d["embedding"] for d in data["data"]]
```

- [ ] **Step 3: Extend `rank.py`**

Add the two functions, then change **one line** in `rank_listings`:

```python
def _text_of(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in
                    ("address", "neighborhood", "property_type", "our_description"))


def embed_listings(listing_ids: list[int]) -> int:
    """Backfill `listing_vec`. No key -> a no-op returning 0, and search stays BM25-only."""
    from . import registry
    emb = registry.embedder()
    if not emb or not listing_ids:
        return 0
    from .db import load_vectors, save_vector
    have, _ = load_vectors(listing_ids)
    todo = [i for i in listing_ids if i not in set(have)]
    if not todo:
        return 0
    holes = ",".join("?" * len(todo))
    with get_conn() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM listing WHERE id IN ({holes})", todo).fetchall()}
    ids = [i for i in todo if i in rows]
    for chunk in (ids[i:i + 64] for i in range(0, len(ids), 64)):
        vecs = emb.embed([_text_of(rows[i]) for i in chunk], input_type="document")
        for i, v in zip(chunk, vecs):
            save_vector(i, v)
    return len(ids)


def cosine_ids(candidate_ids: list[int], query_text: str) -> list[int]:
    """Brute-force `M @ q` in numpy: 0.84ms over 5000x1024. No vector index, no extension,
    no failure mode on a stock python."""
    from . import registry
    emb = registry.embedder()
    if not emb or not candidate_ids or not query_text.strip():
        return []
    import numpy as np

    from .db import load_vectors
    ids, M = load_vectors(candidate_ids)
    if not ids:
        return []
    q = np.asarray(emb.embed([query_text], input_type="query")[0], dtype=np.float32)
    n = float(np.linalg.norm(q))
    if n:
        q = q / n
    sims = M @ q                                   # both sides L2-normed -> cosine
    order = np.argsort(-sims)
    return [ids[i] for i in order]
```

In `rank_listings`, replace:

```python
    lists = [ids for ids in (bm25_ids(candidate_ids, q.keywords),) if ids]
    # Task 12 appends the cosine list here; RRF's signature does not change.
```

with:

```python
    # Keyless: cosine_ids returns [] and RRF fuses ONE list, which is order-preserving —
    # so there is no `if voyage_key` branch anywhere in the ranker.
    query_text = " ".join([*q.keywords, q.neighborhood, *q.property_types]).strip()
    lists = [ids for ids in (bm25_ids(candidate_ids, q.keywords),
                             cosine_ids(candidate_ids, query_text)) if ids]
```

Then have the crawler and the seeder backfill vectors. In `crawl.run`, after
`save_listing(rec)` / `score.enrich(lid)` inside the loop, collect the ids and after the
loops add:

```python
        from . import rank
        rank.embed_listings([lid for lid in saved_ids])   # no-op without a key
```

(declare `saved_ids: list[int] = []` at the top of `run()` and `saved_ids.append(lid)`
where the listing is saved).

- [ ] **Step 4: Extend `tests/test_rank.py`**

```python
def test_cosine_is_a_noop_without_a_key():
    """The keyless invariant: no key -> no cosine list -> RRF over one list -> the exact
    BM25 order. If this ever fails, the ranker grew a branch it doesn't need."""
    os.environ["VOYAGE_API_KEY"] = ""
    from app import registry
    registry.reset()
    ids = _setup()
    assert rank.cosine_ids(list(ids.values()), "wynwood retail") == []
    assert rank.embed_listings(list(ids.values())) == 0

    q = ListingQuery(keywords=["Wynwood", "retail"])
    fused = [r["id"] for r in rank.rank_listings(list(ids.values()), q)]
    bm25 = rank.bm25_ids(list(ids.values()), q.keywords)
    assert fused[:len(bm25)] == bm25, (fused, bm25)


def test_vector_round_trip_is_l2_normalized():
    import numpy as np
    ids = _setup()
    db.save_vector(ids["a"], [3.0, 4.0] + [0.0] * 1022)   # norm 5 -> should store 0.6, 0.8
    got, M = db.load_vectors([ids["a"]])
    assert got == [ids["a"]]
    assert abs(float(np.linalg.norm(M[0])) - 1.0) < 1e-5
    assert abs(float(M[0][0]) - 0.6) < 1e-5
```

- [ ] **Step 5: Run keyless, then (if a key exists) keyed**

```bash
.venv/bin/python -m pytest tests/test_rank.py -v
```

Expected: all passed — including the two new tests, which run **without** a Voyage key.

With a key, prove the fusion changes the order and that the corpus embeds for ~free:

```bash
VOYAGE_API_KEY=pa-... .venv/bin/python -c "
from app import db, rank, registry, seed
seed.seed(); registry.reset()
with db.get_conn() as c:
    ids = [r['id'] for r in c.execute('SELECT id FROM listing').fetchall()]
print('embedded:', rank.embed_listings(ids))
print('cosine top:', rank.cosine_ids(ids, 'somewhere I can open a coffee shop')[:3])
"
```

Expected: 12 embedded; the cosine top hits are the *retail* listings — a query with no
keyword overlap at all, which is precisely what BM25 cannot do.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(openlease): optional Voyage cosine fused into the same RRF call — keyless path unchanged"
```

> **Correction (Task 12):** implementing this section verbatim reproduces the exact bug
> Task 3's ai.py correction already named — every task so far has found one, and this one
> did too.
>
> 1. **`voyage.py`'s `cached()` call never passed `cost_cents`.** As written above,
>    `cached("voyage", input_type, {...}, fetch)` defaults `cost_cents` to 0 — a FREE
>    call — so Voyage (a paid surface, spec §6/§8, even though its free tier covers this
>    corpus 400x) never went through the monthly-budget guardrail at all:
>    `cache.BudgetExceeded` could never be raised, mirroring precisely the pre-fix
>    Anthropic bug two tasks up. Fixed: `voyage.py` now defines `_EMBED_COST_CENTS = 1`
>    (derivation in the code comment — the lite tier's rate over a ~64-listing batch or a
>    short query phrase, rounded up for headroom) and passes
>    `cached(..., cost_cents=_EMBED_COST_CENTS)`.
> 2. **`embed_listings`/`cosine_ids` never caught `cache.BudgetExceeded`.** With `cost_cents`
>    now wired, a real cap-exhaustion would propagate an uncaught exception straight out of
>    `embed_listings` (crashing the crawler's backfill loop) or `cosine_ids` (crashing a live
>    search request) — the opposite of "loudly logged, never a crash" this file states as a
>    Global Constraint. Fixed: both functions wrap their `emb.embed(...)` call in
>    `try/except cache.BudgetExceeded`, log at WARNING naming the budget as the reason
>    (matching `ai.py`'s established pattern exactly), and degrade — `embed_listings`
>    returns however many listings it embedded before the cap hit (not a bare 0, so partial
>    progress survives; whatever's already saved is kept and the rest is picked up next
>    run), `cosine_ids` returns `[]` so `rank_listings`' RRF fuses over BM25 alone, same as
>    the no-key path. Covered by `test_embed_listings_falls_back_loudly_on_budget_exceeded`
>    and `test_cosine_falls_back_loudly_on_budget_exceeded` in `tests/test_rank.py`, both of
>    which trigger the REAL `cache.BudgetExceeded` (via `monthly_budget_cents=0`) rather
>    than a hand-rolled stand-in, so the assertion on the logged text can only pass through
>    the genuine code path.
> 3. **The crawler/seeder wiring in this step was NOT done in this pass.** `app/crawl.py`
>    and `app/seed.py` were out of this task's file scope (kept clear of another task's
>    concurrent edits to those files), so `crawl.run` does not yet call
>    `rank.embed_listings(saved_ids)` and neither does `seed.seed()`. `rank.embed_listings`
>    itself is fully implemented and tested; only its call site at the end of a crawl/seed
>    run is outstanding. Whoever next touches `crawl.py`/`seed.py` should add the ~3-line
>    call this step already describes (`saved_ids: list[int] = []` at the top of `run()`,
>    `saved_ids.append(lid)` where a listing is saved, `rank.embed_listings(saved_ids)`
>    after the loop) — it is a no-op without a key, so it is safe to add at any time.

---

### Task 13: The workspace — saves, portfolios, export, AI highlights, per-listing chat

**Files:**
- Create: `app/routes_portfolios.py`, `app/routes_export.py`, `app/export.py`
- Modify: `app/ai.py` (highlights + `ask`), `app/routes_listings.py` (`/ask`), `app/db.py` (workspace tables), `app/templates/listing.html`, `_listing_card.html`
- Create: `app/templates/portfolios.html`, `_chat.html`
- Test: extend `tests/test_smoke.py`

**Interfaces:**
- Consumes: everything prior.
- Produces:
  - `db.toggle_save(listing_id) -> bool`, `db.list_saved(metro) -> list[dict]`
  - `db.create_portfolio(name) -> int`, `db.add_to_portfolio(pid, listing_id)`, `db.list_portfolios()`, `db.portfolio_items(pid)`
  - `db.add_chat(listing_id, role, content)`, `db.chat_history(listing_id) -> list[dict]`
  - `ai.highlights(listing: dict) -> list[str] | None`, `ai.ask(listing: dict, question: str, history: list[dict]) -> str`
  - `export.to_csv(rows) -> bytes`, `export.to_xlsx(rows) -> bytes`
  - `POST /api/listings/{id}/ask`, `POST /listings/{id}/save`, `GET/POST /portfolios`, `GET /export.{csv,xlsx}`

- [ ] **Step 1: Append the workspace tables to `db.SCHEMA` and add the helpers**

```sql
CREATE TABLE IF NOT EXISTS saved (
    listing_id INTEGER PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    saved_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS portfolio (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS portfolio_item (
    portfolio_id INTEGER REFERENCES portfolio(id) ON DELETE CASCADE,
    listing_id   INTEGER REFERENCES listing(id) ON DELETE CASCADE,
    PRIMARY KEY (portfolio_id, listing_id)
);
CREATE TABLE IF NOT EXISTS chat (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER REFERENCES listing(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,     -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_listing ON chat(listing_id);
```

```python
def toggle_save(listing_id: int) -> bool:
    """Returns the NEW saved state."""
    with get_conn() as conn:
        hit = conn.execute("SELECT 1 FROM saved WHERE listing_id = ?", (listing_id,)).fetchone()
        if hit:
            conn.execute("DELETE FROM saved WHERE listing_id = ?", (listing_id,))
            return False
        conn.execute("INSERT INTO saved (listing_id) VALUES (?)", (listing_id,))
        return True


def is_saved(listing_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM saved WHERE listing_id = ?",
                            (listing_id,)).fetchone() is not None


def list_saved(metro: str | None = None) -> list[dict]:
    sql = ("SELECT l.* FROM listing l JOIN saved s ON s.listing_id = l.id "
           + ("WHERE l.metro = ? " if metro else "") + "ORDER BY s.saved_at DESC")
    with get_conn() as conn:
        rows = conn.execute(sql, (metro,) if metro else ()).fetchall()
    return [dict(r) for r in rows]


def create_portfolio(name: str) -> int:
    with get_conn() as conn:
        return conn.execute("INSERT INTO portfolio (name) VALUES (?) RETURNING id",
                            (name,)).fetchone()["id"]


def add_to_portfolio(portfolio_id: int, listing_id: int) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO portfolio_item (portfolio_id, listing_id) VALUES (?, ?) "
                     "ON CONFLICT DO NOTHING", (portfolio_id, listing_id))


def list_portfolios() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.created_at, COUNT(i.listing_id) AS n "
            "FROM portfolio p LEFT JOIN portfolio_item i ON i.portfolio_id = p.id "
            "GROUP BY p.id ORDER BY p.created_at DESC").fetchall()
    return [dict(r) for r in rows]


def portfolio_items(portfolio_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT l.* FROM listing l JOIN portfolio_item i ON i.listing_id = l.id "
            "WHERE i.portfolio_id = ?", (portfolio_id,)).fetchall()
    return [dict(r) for r in rows]


def add_chat(listing_id: int, role: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO chat (listing_id, role, content) VALUES (?, ?, ?)",
                     (listing_id, role, content))


def chat_history(listing_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat WHERE listing_id = ? ORDER BY id",
            (listing_id,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 2: Add `highlights` and `ask` to `ai.py`**

```python
def _listing_facts(l: dict) -> str:
    """The grounding context for chat and highlights. Note what is NOT here: the broker's
    prose. We never had it, so the model can never launder it back out."""
    import json as _json
    keep = ["address", "neighborhood", "borough", "property_type", "transaction_type",
            "size_sf", "divisible_min_sf", "divisible_max_sf", "floor", "ceiling_height_ft",
            "asking_rent", "rent_unit", "lease_type", "sale_price", "availability_date",
            "lease_term_months", "condition", "walk_score", "transit_score",
            "broker_name", "broker_firm", "our_description"]
    lines = [f"{k}: {l[k]}" for k in keep if l.get(k) is not None]
    if l.get("score_breakdown_json"):
        b = _json.loads(l["score_breakdown_json"])
        lines.append("walkability by category: " + ", ".join(
            f"{cat} {v['count']} within 1.5mi (nearest {v['nearest_m']}m)"
            for cat, v in b.items() if v["count"]))
    return "\n".join(lines)


def highlights(l: dict) -> list[str] | None:
    """3-5 bullets, generated ONCE from the facts and cached on the listing row (the
    caller, routes_listings.listing_page, only invokes this when highlights_json is still
    empty). This is also how we avoid ever needing the broker's copy — we write our own.

    The paid call goes through cache.cached() like every other paid surface in this
    module — it's the only place the monthly budget cap can be enforced, and a
    refused-by-budget call degrades to "no highlights" exactly like any other fallback:
    LOUDLY logged, never silent."""
    if not available():
        return None
    facts = _listing_facts(l)
    req = {"listing_id": l.get("id"), "facts": facts, "model": settings.llm_model}

    def fetch():
        resp = _client().messages.create(
            model=settings.llm_model, max_tokens=400,
            system=("Write 3-5 short bullets a tenant rep would actually care about, FROM "
                    "THE FACTS below. One line each, prefixed '- '. No marketing language, "
                    "no adjectives you cannot source from the data. If a fact is absent, "
                    "do not invent it."),
            messages=[{"role": "user", "content": facts}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")][:5]

    try:
        bullets = cache.cached("anthropic", "messages.create.highlights", req, fetch,
                               cost_cents=_HIGHLIGHTS_COST_CENTS)
        return bullets or None
    except cache.BudgetExceeded as e:
        log.warning(
            "AI highlights skipped for listing %s — monthly paid-spend cap reached (%s); "
            "the listing page shows no highlights instead of failing.", l.get("id"), e
        )
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("highlights failed (%s): %s", type(e).__name__, e)
        return None


def ask(l: dict, question: str, history: list[dict]) -> str:
    """Per-listing RAG chat. NO chunking, no vector store: the enriched record fits in one
    prompt, so 'retrieval' is one SELECT (db.get_listing). Grounded — if it isn't in the
    record, say so; a tool that guesses is worse than one that admits it doesn't know.

    Same cache.cached()/budget-cap discipline as nl_to_query/reply/highlights above."""
    if not available():
        return ("Chat needs an ANTHROPIC_API_KEY — paste one on the settings page. "
                "Everything else on this page works without it.")
    facts = _listing_facts(l)
    trimmed_history = [{"role": h["role"], "content": h["content"]} for h in history[-8:]]
    req = {"listing_id": l.get("id"), "question": question, "history": trimmed_history,
           "facts": facts, "model": settings.llm_model}

    def fetch():
        resp = _client().messages.create(
            model=settings.llm_model, max_tokens=800,
            system=("You are answering a tenant rep's question about ONE commercial listing. "
                    "Answer ONLY from the record below. If the record does not contain the "
                    "answer, say plainly that this listing does not publish it and suggest "
                    "asking the broker — never guess a number.\n\n"
                    f"RECORD:\n{facts}"),
            messages=[*trimmed_history, {"role": "user", "content": question}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

    try:
        return cache.cached("anthropic", "messages.create.ask", req, fetch,
                            cost_cents=_ASK_COST_CENTS)
    except cache.BudgetExceeded as e:
        log.warning(
            "Per-listing chat skipped for listing %s — monthly paid-spend cap reached "
            "(%s); telling the user instead of silently failing.", l.get("id"), e
        )
        return ("This month's AI budget cap has been reached — check /settings, or ask "
                "again next month.")
    except Exception as e:  # noqa: BLE001
        log.warning("listing chat failed (%s): %s", type(e).__name__, e)
        return "That request failed. Check the key on /settings, or try again."
```

Both cost constants are sized the same way `_PARSE_COST_CENTS`/`_REPLY_COST_CENTS` are —
see the comments above them in `ai.py`: `_HIGHLIGHTS_COST_CENTS = 1`, `_ASK_COST_CENTS = 2`.

> **Correction (Task 13, verified live):** the brief's first draft had `highlights()` and
> `ask()` call `_client().messages.create()` directly — the ONLY two paid Anthropic
> surfaces in the whole app that would NOT go through `cache.cached()`. Every other paid
> call (`nl_to_query`'s `messages.parse`, `reply`'s `messages.create`) is gated by the
> monthly budget cap via `cache.cached(..., cost_cents=...)` — that's the entire point of
> `cache.py`'s own docstring ("every provider call goes through cached()"). As drafted, a
> user could burn unlimited real spend just by opening listing pages (`highlights`) or
> asking questions (`ask`) after the monthly cap was already exhausted, with zero
> enforcement and zero warning — exactly the kind of silent gap the budget guardrail
> exists to close. Fixed in the block above: both now build a `req` dict and route the
> paid call through `cache.cached()`, with a `cache.BudgetExceeded` branch that degrades
> the same way every other fallback in this file does — LOUDLY logged at WARNING, never
> silent, never a crash. This also gives both calls "never pay twice" caching for an
> identical repeated question, same as `nl_to_query`. Verified: `test_ai.py`'s
> `test_highlights_budget_exceeded_falls_back_to_none_and_logs_loudly` and
> `test_ask_budget_exceeded_falls_back_honestly_and_logs_loudly` reproduce the gap against
> the pre-fix code (both fail: the mocked client that must not be called IS called) and
> pass against the fix above.

- [ ] **Step 3: Write `export.py`** (lifted from OpenProp, CRE columns)

```python
"""CSV / XLSX export of a result set. stdlib csv for CSV; openpyxl for XLSX. The column
set is what a tenant rep actually sends a client — and note what is NOT exportable:
the broker's prose (we never stored it) and their photos (we never downloaded them)."""
import csv
import io

FIELDS = [
    "address", "neighborhood", "borough", "metro", "property_type", "transaction_type",
    "size_sf", "divisible_min_sf", "divisible_max_sf", "floor", "ceiling_height_ft",
    "asking_rent", "rent_unit", "lease_type", "sale_price", "availability_date",
    "walk_score", "transit_score", "broker_name", "broker_firm", "broker_phone",
    "our_description", "source", "source_url",
]


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def to_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "OpenLease export"
    ws.append(FIELDS)
    for r in rows:
        ws.append([r.get(f) for f in FIELDS])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
```

- [ ] **Step 4: Write `routes_portfolios.py` and `routes_export.py`**

```python
"""Saves + client shortlists."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db
from .app import app, require_auth, spend_ctx, templates
from .models import to_api


@app.post("/listings/{listing_id}/save", response_class=HTMLResponse)
def save_toggle(listing_id: int, _=Depends(require_auth)):
    on = db.toggle_save(listing_id)
    return HTMLResponse(
        f'<button hx-post="/listings/{listing_id}/save" hx-swap="outerHTML" '
        f'class="rounded border px-3 py-1 text-xs '
        f'{"bg-sky-600 text-white" if on else "text-slate-600"}">'
        f'{"Saved" if on else "Save"}</button>'
    )


@app.get("/portfolios", response_class=HTMLResponse)
def portfolios_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "portfolios.html", {
        "portfolios": db.list_portfolios(),
        "saved": [to_api(r) for r in db.list_saved()],
        **spend_ctx(),
    })


@app.post("/portfolios", response_class=HTMLResponse)
def portfolio_create(request: Request, name: str = Form(...), _=Depends(require_auth)):
    db.create_portfolio(name)
    return portfolios_page(request, True)


@app.post("/portfolios/{portfolio_id}/add")
def portfolio_add(portfolio_id: int, listing_id: int = Form(...), _=Depends(require_auth)):
    db.add_to_portfolio(portfolio_id, listing_id)
    return {"ok": True}
```

> **Correction (Task 13, verified live):** the brief's first draft of `portfolios_page`
> called `templates.TemplateResponse("portfolios.html", {"request": request, ...})` — the
> deprecated Starlette signature (name first, `request` folded into the context dict) that
> Task 1's own correction (see above, ~line 507) already fixed everywhere else in the app
> and that the project treats as a hard bar ("zero warnings" — a standing
> `DeprecationWarning` on every `/portfolios` render would mask a real new one). Fixed in
> the block above: `TemplateResponse(request, "portfolios.html", {...})`, `request` as the
> first positional argument, dropped from the context dict. Verified:
> `.venv/bin/python -m pytest tests/ -v -W error` stays at 0 warnings with this route live.

```python
"""CSV / XLSX of the saved set or a portfolio."""
from fastapi import Depends
from fastapi.responses import Response

from . import db, export
from .app import app, require_auth


def _rows(portfolio_id: int | None):
    return db.portfolio_items(portfolio_id) if portfolio_id else db.list_saved()


@app.get("/export.csv")
def export_csv(portfolio_id: int | None = None, _=Depends(require_auth)):
    return Response(export.to_csv(_rows(portfolio_id)), media_type="text/csv",
                    headers={"content-disposition": 'attachment; filename="openlease.csv"'})


@app.get("/export.xlsx")
def export_xlsx(portfolio_id: int | None = None, _=Depends(require_auth)):
    return Response(
        export.to_xlsx(_rows(portfolio_id)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"content-disposition": 'attachment; filename="openlease.xlsx"'})
```

Add both to `app.py`'s route-import block:

```python
from . import routes_portfolios  # noqa: E402,F401  (T13)
from . import routes_export      # noqa: E402,F401  (T13)
```

- [ ] **Step 5: Add the chat endpoint and the listing-page chat panel**

In `routes_listings.py`:

```python
from pydantic import BaseModel


class AskBody(BaseModel):
    question: str
    history: list[dict] = []


@app.post("/api/listings/{listing_id}/ask")
def api_ask(listing_id: int, body: AskBody, _=Depends(require_auth)):
    from . import ai
    row = db.get_listing(listing_id)
    if not row:
        return {"error": "not found"}
    history = body.history or db.chat_history(listing_id)
    answer = ai.ask(row, body.question, history)
    db.add_chat(listing_id, "user", body.question)
    db.add_chat(listing_id, "assistant", answer)
    return {"answer": answer, "history": db.chat_history(listing_id)}


@app.post("/listings/{listing_id}/ask", response_class=HTMLResponse)
def ask_fragment(request: Request, listing_id: int, question: str = Form(...),
                 _=Depends(require_auth)):
    from . import ai
    api_ask(listing_id, AskBody(question=question), True)
    return templates.TemplateResponse(
        request, "_chat.html", {"listing_id": listing_id, "ai_available": ai.available(),
                                "history": db.chat_history(listing_id)})
```

> **Correction (Task 13, verified live):** same `TemplateResponse` signature bug as
> `portfolios_page` above — the brief's first draft passed `"_chat.html"` first with
> `request` folded into the context dict. Fixed in the block above (`request` as the first
> positional argument). Also added `ai_available` to the context — see the `_chat.html`
> correction immediately below for why.

`templates/_chat.html`:

```html
<div id="chat">
  {% if not ai_available and not history %}
  <p class="mb-2 text-xs text-slate-400">
    Chat needs an ANTHROPIC_API_KEY — add one on <a class="text-sky-600 hover:underline"
    href="/settings">/settings</a> to enable this. Everything else on this page works
    without it.</p>
  {% endif %}
  {% for m in history %}
  <div class="mb-2 text-sm {{ 'text-slate-800' if m.role == 'assistant' else 'text-slate-500' }}">
    <span class="text-[10px] uppercase tracking-wide text-slate-400">{{ m.role }}</span>
    <p class="whitespace-pre-line">{{ m.content }}</p>
  </div>
  {% endfor %}
  <form hx-post="/listings/{{ listing_id }}/ask" hx-target="#chat" hx-swap="outerHTML"
        class="mt-2 flex gap-2">
    <input name="question" required placeholder="Ask about this space…"
           class="flex-1 rounded border-slate-300 text-sm py-1.5">
    <button class="rounded bg-slate-800 px-3 py-1.5 text-xs text-white">Ask</button>
  </form>
</div>
```

> **Correction (Task 13, verified live):** constraint #3 ("Highlights and per-listing chat
> ... with no key they must degrade honestly and LOUDLY — a clear 'add an Anthropic key in
> Settings to enable this' in the UI") is only half-satisfied by the AFTER-the-fact gate
> message that `ai.ask()` returns once someone actually asks a question (it shows up as an
> assistant turn in `history`, via the existing keyless-fallback string). Before the first
> question, the panel looked identical whether or not a key was configured — a user had to
> spend a click to discover chat was disabled. Added the `{% if not ai_available and not
> history %}` notice above so the gate is visible UP FRONT, not just after wasting an ask.

In `listing.html`, inside `{% block listing_extra %}` (after the score breakdown), add the
save button, an "add to portfolio" control per existing portfolio, the highlights (or the
keyless gate notice), and the chat panel:

```html
<div class="rounded-lg border bg-white p-4">
  <div class="flex items-center justify-between mb-2">
    <h2 class="text-sm font-semibold">Ask about this space</h2>
    <button hx-post="/listings/{{ l.id }}/save" hx-swap="outerHTML"
            class="rounded border px-3 py-1 text-xs {{ 'bg-sky-600 text-white' if saved else 'text-slate-600' }}">
      {{ 'Saved' if saved else 'Save' }}</button>
  </div>

  {% if portfolios %}
  <div class="mb-3 flex flex-wrap items-center gap-2 text-xs">
    <span class="text-slate-400">Add to portfolio:</span>
    {% for p in portfolios %}
    <span class="inline-flex items-center gap-1">
      <button hx-post="/portfolios/{{ p.id }}/add" hx-vals='{"listing_id": {{ l.id }}}'
              hx-swap="none" onclick="this.nextElementSibling.classList.remove('hidden')"
              class="rounded border px-2 py-1 text-slate-600 hover:border-sky-400">
        + {{ p.name }}</button>
      <span class="hidden text-sky-600">added</span>
    </span>
    {% endfor %}
  </div>
  {% else %}
  <p class="mb-3 text-xs text-slate-400">
    No portfolios yet — <a class="text-sky-600 hover:underline" href="/portfolios">create one</a>
    to build a client shortlist.</p>
  {% endif %}

  {% if l.highlights %}
  <ul class="mb-3 list-disc pl-5 text-sm text-slate-700">
    {% for h in l.highlights %}<li>{{ h }}</li>{% endfor %}
  </ul>
  {% elif not ai_available %}
  <p class="mb-3 text-xs text-slate-400">
    Highlights need an ANTHROPIC_API_KEY — add one on <a class="text-sky-600 hover:underline"
    href="/settings">/settings</a> to enable this.</p>
  {% endif %}
  {% include "_chat.html" %}
</div>
```

> **Correction (Task 13, verified live):** two gaps found against the task's own
> requirements while building the live keyless walkthrough:
>
> 1. **No UI ever calls `POST /portfolios/{portfolio_id}/add`.** The brief defines the
>    endpoint (Step 4) but neither `listing.html` nor `portfolios.html` renders anything
>    that posts to it — "add it to a portfolio for your client" (the task's own framing of
>    this feature) had no way to happen through the browser, only via a raw HTTP call.
>    Added the "Add to portfolio" button row above: one small button per existing
>    portfolio, `hx-vals` carrying the current listing's id, `hx-swap="none"` (the route
>    returns `{"ok": true}`, not HTML) with a same-tick `onclick` reveal of a small "added"
>    label for immediate feedback. Requires `portfolios=db.list_portfolios()` in
>    `listing_page`'s context (added below).
> 2. **Highlights had no keyless notice at all** — `{% if l.highlights %}` simply renders
>    nothing when there's no key, which is honest but not LOUD (constraint #3 again). Added
>    the `{% elif not ai_available %}` branch. Requires `ai_available=ai.available()` in
>    `listing_page`'s context (added below).

and pass `saved=db.is_saved(listing_id)`, `history=db.chat_history(listing_id)`,
`listing_id=listing_id`, `portfolios=db.list_portfolios()`, and `ai_available=ai.available()`
into `listing_page`'s context. Generate highlights lazily there:

```python
    from . import ai
    ...
    if not row.get("highlights_json"):
        hl = ai.highlights(row)
        if hl:
            with db.get_conn() as conn:
                conn.execute("UPDATE listing SET highlights_json = ? WHERE id = ?",
                             (json.dumps(hl), listing_id))
            row["highlights_json"] = json.dumps(hl)
```

> **Correction:** `import json` moved to the top of `routes_listings.py` (already imported
> there for `/search`'s `priorState` parsing) instead of a redundant inline import inside
> this block.

`_listing_card.html` also needed one fix, not called out anywhere above: its rationale line
was `<p ...>{{ l.rationale }}</p>`, unguarded. `l.rationale` is computed PER QUERY by
`rank.py` and was never persisted on the row — every listing reached via `db.list_saved()`
or `db.portfolio_items()` (no ranking pass) has `rationale = NULL`. Jinja renders a
defined-but-`None` value as the literal string `"None"` (confirmed: `Template("<p>{{
l.rationale }}</p>").render(l={"rationale": None})` → `"<p>None</p>"`), which would have
shown up as a stray "None" under every card on the new `/portfolios` page — exactly the
"`None` ≠ 0 ≠ 'lookup failed'" class of bug the constraints call out, just showing up in a
template instead of a provider. Fixed: `{% if l.rationale %}<p ...>{{ l.rationale }}</p>{%
endif %}`. Covered by
`test_saved_listing_card_never_prints_python_none_for_missing_rationale` in
`tests/test_smoke.py`.

- [ ] **Step 6: Write `templates/portfolios.html`**

```html
{% extends "base.html" %}
{% block title %}Portfolios — OpenLease{% endblock %}
{% block content %}
<div class="flex items-center justify-between mb-4">
  <h1 class="text-xl font-semibold">Saved &amp; portfolios</h1>
  <div class="flex gap-2 text-sm">
    <a href="/export.csv" class="rounded border px-3 py-1.5">Export CSV</a>
    <a href="/export.xlsx" class="rounded border px-3 py-1.5">Export XLSX</a>
  </div>
</div>

<form method="post" action="/portfolios" class="mb-6 flex gap-2">
  <input name="name" required placeholder="New client shortlist…"
         class="rounded border-slate-300 text-sm py-1.5">
  <button class="rounded bg-sky-600 px-3 py-1.5 text-sm text-white">Create</button>
</form>

{% if portfolios %}
<ul class="mb-6 space-y-1 text-sm">
  {% for p in portfolios %}
  <li class="flex justify-between rounded border bg-white px-3 py-2">
    <span>{{ p.name }}</span>
    <span class="text-slate-400">{{ p.n }} listing{{ '' if p.n == 1 else 's' }}
      · <a class="text-sky-600" href="/export.csv?portfolio_id={{ p.id }}">csv</a></span>
  </li>
  {% endfor %}
</ul>
{% endif %}

<h2 class="mb-2 text-sm font-semibold">Saved ({{ saved|length }})</h2>
{% for l in saved %}{% include "_listing_card.html" %}{% endfor %}
{% if not saved %}<p class="text-sm text-slate-500">Nothing saved yet.</p>{% endif %}
{% endblock %}
```

Add a `portfolios` link to `base.html`'s header, beside `settings`:

```html
        <a href="/portfolios" class="text-slate-400 hover:text-slate-700">saved</a>
```

- [ ] **Step 7: Extend `tests/test_smoke.py`**

```python
def test_workspace_save_portfolio_export_and_chat_gate():
    from app import db, seed
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://nyc/1'").fetchone()["id"]

        r = c.post(f"/listings/{lid}/save")
        assert r.status_code == 200 and "Saved" in r.text
        assert db.is_saved(lid) is True
        c.post(f"/listings/{lid}/save")                 # toggles back off
        assert db.is_saved(lid) is False
        c.post(f"/listings/{lid}/save")

        r = c.post("/portfolios", data={"name": "Acme Corp"})
        assert r.status_code == 200 and "Acme Corp" in r.text

        r = c.get("/export.csv")
        assert r.status_code == 200
        head, first = r.text.splitlines()[0], r.text.splitlines()[1]
        assert "our_description" in head and "source_url" in head
        assert "55 Gansevoort St" in first
        # what CANNOT be exported, because it was never stored:
        assert "photo" not in head.lower()

        r = c.get("/export.xlsx")
        assert r.status_code == 200 and r.content[:2] == b"PK"   # a real zip/xlsx

        # chat with no key: the gate says so instead of failing
        r = c.post(f"/api/listings/{lid}/ask", json={"question": "what's the ceiling height?"})
        assert r.status_code == 200
        assert "ANTHROPIC_API_KEY" in r.json()["answer"]
```

> **Correction (Task 13, verified live):** the single test above never exercises
> `POST /portfolios/{id}/add` (the "add it to a portfolio" workflow itself), never proves
> the `_listing_card.html` rationale-None fix, and never proves the new `ai.py` functions
> are grounded/budget-capped rather than just gated on `available()`. Four more tests
> added to `tests/test_smoke.py` (`test_portfolio_add_and_scoped_export`,
> `test_saved_listing_card_never_prints_python_none_for_missing_rationale`,
> `test_listing_page_shows_highlights_gate_and_saved_state`) and seven to `tests/test_ai.py`
> (keyless-honest, budget-exceeded-loudly, and fake-keyed grounded-answer/cache-hit tests
> for both `highlights()` and `ask()`, mirroring the existing `nl_to_query`/`reply` budget
> tests). All hermetic — no live Anthropic calls; a fake `_client()` stands in wherever a
> keyed response is needed.

- [ ] **Step 8: Run to green**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all passed.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(openlease): workspace — saves, portfolios, CSV/XLSX export, AI highlights, per-listing RAG chat"
```

---

### Task 14: README, guide, license, repo wiring

The repo is the storefront: a buyer must be able to go from `git clone` to a working search
without reading the code.

**Files:**
- Create: `README.md`, `guide/OpenLease-Setup-Guide.html`, `guide/build-pdf.sh`, `LICENSE.md`
- Modify: root `README.md` (add OpenLease beside OpenProp)
- Test: `tests/test_smoke.py` (already covers the keyless path the README promises)

- [ ] **Step 1: Write `README.md`**

````markdown
# OpenLease

An AI-native commercial-real-estate leasing search you host yourself. Describe the space
you need in plain English — *"retail in Wynwood ~1,500 SF under $8k/mo"* — and get matching
listings on a map, each enriched with free public data and conversationally queryable.

Four markets: **New York, Miami, Los Angeles, Chicago**.

## It runs with no API keys at all

Everything below is free, keyless, and government-sourced:

| Works with no key | Unlocked by a key you bring |
|---|---|
| Parcel data — all four metros | `ANTHROPIC_API_KEY` — plain-English search, conversational replies, per-listing chat, LLM extraction |
| Walk Score + Transit Score (published methodology, from OpenStreetMap) | `VOYAGE_API_KEY` — semantic ranking (the free tier covers this corpus ~400×) |
| Bundled transit stations, airport drive times | `GOOGLE_MAPS_KEY` — Street View embed |
| **Full-text search + ranking**, the crawler, CSV import | |
| Portfolios, saves, export, map | |

Keys are pasted on the **Settings** page, not into a file. Paid calls are capped by a
monthly budget you set, and every response is cached — you never pay for the same call twice.

## Run it

**macOS:** double-click **Start OpenLease.command**. That's it.
**Windows:** double-click **Start OpenLease.bat**.

Or by hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set OPENLEASE_PASSWORD
.venv/bin/python -m app.seed  # 12 demo listings, so there's something to search
./run.sh                      # -> http://localhost:8788
```

Docker: `docker compose up` → http://localhost:8788

OpenLease runs on **8788**, so it and [OpenProp](https://github.com/dweng1572179/openprop) (8787) can run side by side.

## Where the listings come from

Three supplies, in order of how much you should trust them:

1. **Free government feeds** — no terms-of-service surface at all. NYC's Storefront
   Registry publishes a *vacancy flag* on every ground-floor commercial space in the city:
   `POST /api/import/storefronts`. That's a lead source no broker site has.
2. **Your own CSV** — a broker export, a CoStar pull, whatever you already licensed:
   `POST /api/import/csv`.
3. **The crawler** — `POST /api/crawl`, over the allowlist in `app/data/sources.yml`.

### What the crawler will and won't do

It obeys `robots.txt` (including `Crawl-delay`), identifies itself honestly, rate-limits to
one request every few seconds per domain, and caches hard.

It **never logs in.** Not to any site, not ever, and there is no flag to make it. Every
scraping case that ended badly ended there.

It stores **facts, not expression**: address, size, ask, type, broker contact, and the link
back to the original. It does **not** copy the broker's marketing prose (the descriptions
you read here are written from the facts by OpenLease) and it does **not** download or
re-host their photos (they're hot-linked from the broker's own server, and the listing page
links you to their page).

This is local-and-personal software. Don't republish what it collects.

## The searching

Plain English goes to an LLM, which turns it into hard constraints (type, size band, rent
cap, bounding box). Those become a SQL `WHERE` — a constraint is a constraint, never a
preference that gets ranked away. Whatever survives is ranked by full-text relevance
(SQLite FTS5/BM25) and, if you brought a Voyage key, semantic similarity, fused with
Reciprocal Rank Fusion. Then the LLM writes the reply.

With no Anthropic key, a rules-based parser takes over. It understands far less — and it
says so, loudly, in the log, rather than quietly dropping half your query.

## The scores

Walk Score is **Walk Score's own published 2011 methodology**, computed here from
OpenStreetMap: nine amenity categories, distance-decayed, normalized to 0–100. It
reproduces their published values (Empire State Building = 100, Bay Ridge = 98), and every
listing page shows the per-category breakdown — it explains the score instead of asserting
it.

Transit Score aggregates **per route**, not per stop. Its normalization constant is
calibrated, not published, so treat it as a ranking rather than a rating.

Airport drive times come from OSRM and are **free-flow — no traffic**. The UI says so.

## What a metro doesn't publish, it says it doesn't publish

LA County does not publish owner-of-record for free (California statute). Chicago's zoning
dataset is the *City's* — it's blank for suburban Cook County. Miami's county zoning layer
returns nothing inside incorporated cities like Wynwood and Brickell, so we branch to the
municipal layer.

In every one of those cases the field reads **"not published here"** with the reason on
hover. It is never a blank, and never a zero. A tool that guesses is worse than a tool that
admits.

## License

PolyForm Noncommercial 1.0.0 — see `LICENSE.md`. Use it for anything except selling it.
````

- [ ] **Step 2: Add the license**

```bash
curl -sL https://polyformproject.org/licenses/noncommercial/1.0.0/ -o /dev/null
cp ../openprop/LICENSE.md LICENSE.md 2>/dev/null || \
  curl -sL https://raw.githubusercontent.com/polyformproject/polyform-licenses/master/PolyForm-Noncommercial-1.0.0.md \
  -o LICENSE.md
head -3 LICENSE.md
```

Expected: the PolyForm Noncommercial 1.0.0 text. (The repo's root `LICENSE.md` already
carries it — reuse that file rather than fetching if it is present.)

- [ ] **Step 3: Build the setup guide**

Copy OpenProp's guide scaffolding and rewrite the content for OpenLease — same CSS, same
`build-pdf.sh`, same structure (what it is → run it → paste keys → first search → where
listings come from → what each metro won't tell you).

```bash
mkdir -p guide/assets
cp ../openprop/guide/assets/guide.css guide/assets/guide.css
cp ../openprop/guide/assets/fintok-logo.jpeg guide/assets/fintok-logo.jpeg
cp ../openprop/guide/build-pdf.sh guide/build-pdf.sh
sed -i '' 's/OpenProp/OpenLease/g; s/openprop/openlease/g' guide/build-pdf.sh
chmod +x guide/build-pdf.sh
```

Write `guide/OpenLease-Setup-Guide.html` following `../openprop/guide/OpenProp-Setup-Guide.html`
section-for-section, with these substitutions: port 8788, `OPENLEASE_PASSWORD`, the keyless
table from the README, the three supplies (government / CSV / crawler), and a **"What the
crawler will not do"** section carrying the never-authenticate and facts-not-expression
rules in plain language. Then:

```bash
cd guide && ./build-pdf.sh && ls -la *.pdf
```

Expected: `OpenLease-Setup-Guide.pdf` exists.

- [ ] **Step 4: Cross-link from the OpenProp README**

OpenLease is its own repo, but OpenProp's readers should be able to find it (same author,
same architecture, adjacent domain). In the **openprop** checkout, add a short "sibling
project" line — what OpenLease is (the open SpaceFinder), the four metros, keyless-first,
port 8788 — linking to `https://github.com/dweng1572179/openlease`. Commit and push that
repo separately.

- [ ] **Step 5: Full verification pass**

```bash
.venv/bin/python -m pytest tests/ -v && \
  .venv/bin/python -m app.config && .venv/bin/python -m app.ai && .venv/bin/python -m app.score
```

Expected: every test passes and all three self-checks print OK.

Then the keyless demo the README promises, start to finish:

```bash
rm -f openlease.db* && .venv/bin/python -m app.seed && ./run.sh
```

Open http://localhost:8788, log in, and run the README's own example search in Miami.
Expect: results, pins, a listing page with a Walk Score breakdown, a parcel panel, a save
button, and no key anywhere. Ctrl-C when done.

- [ ] **Step 6: Commit and push**

```bash
git add -A
git commit -m "docs(openlease): README, setup guide + PDF, license, and the root README entry"
git push
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task:

| Spec | Task |
|---|---|
| §2 Layer 1 — fetch ladder, Scrapling, guardrails, `sources.yml` | T10 |
| §2 Layer 1 — free government supply (Storefront, ACRIS, CSV) | T11 |
| §2 Layer 2 — parcel providers ×4, `null`-with-a-reason | T9 |
| §2 Layer 2 — Overpass, Walk/Transit Score, rail bundles, OSRM, Street View | T7, T8 |
| §2 Layer 3 — parse (sentinels), filter, RRF rank, reply | T4, T5, T3, T12 |
| §2 Layer 4 — RAG chat, highlights, portfolios, export, map | T13, T6 |
| §3 Architecture / §4 Layout | T1–T14 (the file map above) |
| §5 Data model + camelCase boundary + the two divergences | T2 (+ each task appends its tables) |
| §6 Keyless vs. keyed + budget guardrail | T1 (cache/settings), enforced in T4/T12 |
| §7 Verified data appendix | T7 (rail, OSRM), T9 (parcels), T10 (`sources.yml`), T11 (gov) |
| §8 Scope boundaries | Nothing in this plan builds anything from the "Out" list |

**Type consistency, checked across tasks:** `ListingQuery` fields are snake_case in Python
and camelCase on the wire (T2), and `ai.QueryExtract.to_query()` (T4), `db.filter_listings`
(T5) and `rank.rank_listings` (T3) all consume the snake_case form. `Parcel.missing_reason`
is a `dict[str, str]` in T2, written by all four providers in T9, persisted as
`missing_reason_json` in T9, and read back as `missing_reason` by `db.get_parcel` — the
listing template (T6) indexes it by field name. `score.enrich` returns
`{"walk_score", "transit_score", "breakdown"}` (T8) and is called by both `crawl.run` (T10)
and `import_storefronts` (T11). `rank.rrf(lists)` takes a **list of lists** in T3 and is
called with two lists in T12 without a signature change.

**Deliberate ordering:** the seed data (T2) exists so search (T5) and the UI (T6) are
testable and demoable *before* the crawler (T10) — each task ends in something you can look
at, not a library waiting for its caller.

**One flagged risk, not a placeholder:** `score.TRANSIT_NORM = 4000.0` and
`score.TRIPS_PER_WEEK` are the only unpublished constants in the app. The spec says they
need calibration against ~20 known addresses. They are marked `ponytail:` in the code with
the upgrade path (read trips/week from each agency's GTFS), the test asserts only the
*ordering* they must produce, and the UI labels Transit Score as a ranking rather than a
rating. Do not quote a Transit Score as gospel until that calibration is done.
