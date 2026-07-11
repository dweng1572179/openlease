"""SQLite: listings + parcels + sessions + cache + workspace. Raw stdlib sqlite3 —
no ORM, no migrations. Schema is spec §5. Single file, WAL mode."""
import json
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

-- External-content FTS5: the text lives once, in `listing`. Triggers keep the index in
-- sync so a re-crawl's upsert (save_listing) reindexes automatically.
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
