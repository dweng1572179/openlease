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

-- float32 BLOBs, L2-normalized at write. Present only with a VOYAGE_API_KEY.
-- Deliberately NOT sqlite-vec: that needs enable_load_extension, which is ABSENT on stock
-- python.org macOS / pyenv / system python. It would work in Docker and break on the
-- user's Mac — the worst failure mode there is — and buys nothing at 5k rows, where a
-- brute-force numpy matmul is 0.84ms (T12).
CREATE TABLE IF NOT EXISTS listing_vec (
    listing_id INTEGER PRIMARY KEY REFERENCES listing(id) ON DELETE CASCADE,
    embedding  BLOB NOT NULL
);

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

-- Overpass results, cached FOREVER. Buildings do not move, and Overpass 429/504s under
-- request-time load -- so this is an INGEST-time fetch, never a search-time one.
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

-- Workspace (T13): saves/favorites, client-shortlist portfolios, per-listing chat.
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


# --- semantic vectors (T12) ----------------------------------------------------
# numpy is a hard requirement (requirements.txt) but imported lazily here so a keyless
# boot never pays for it before a listing is actually embedded.

def save_vector(listing_id: int, vec) -> None:
    """Store `vec` L2-normalized as a float32 BLOB, so `cosine_ids`'s brute-force
    `M @ q` is a plain dot product (both sides already unit-length) — no per-query norm."""
    import numpy as np
    arr = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if n:
        arr = arr / n                      # L2-normalize at WRITE
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO listing_vec (listing_id, embedding) VALUES (?, ?) "
            "ON CONFLICT(listing_id) DO UPDATE SET embedding = excluded.embedding",
            (listing_id, arr.tobytes()),
        )


def load_vectors(ids: list[int]):
    """-> (ids_present, M) where M is (n, dim) float32, row i = ids_present[i]. Ids with
    no stored vector are simply absent, never a zero row."""
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
    # All four or none: 0 is the sentinel for these, so a partial bbox fails the
    # conjunction and is skipped entirely. A half-formed box is a silently wrong
    # geographic filter — worse than no filter. (ai.to_query drops it as a group too.)
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


# --- workspace: saves, portfolios, per-listing chat (T13) ----------------------

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


# --- the enrichment the listing page shows (poi / transit_nearby are written by
#     score.enrich and, until now, never read by anything) -------------------------------

def nearby_pois(listing_id: int, per_category: int = 3) -> dict[str, list[dict]]:
    """The closest few POIs in each category, with real distances. This is the enrichment
    SpaceFinder puts on every listing page — and we were computing it, storing it, and
    then never showing it."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, name, meters FROM poi WHERE listing_id = ? AND name IS NOT NULL "
            "ORDER BY category, meters", (listing_id,)
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["category"], [])
        if len(bucket) < per_category:
            bucket.append({"name": r["name"], "meters": int(r["meters"])})
    return out


def nearby_transit(listing_id: int, limit: int = 6) -> list[dict]:
    """Nearest stations/stops, closest first. Rail before bus at equal distance — a subway
    line is worth more to a tenant than a bus stop, and the Transit Score already says so."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mode, route, name, meters FROM transit_nearby WHERE listing_id = ? "
            "ORDER BY meters LIMIT ?", (listing_id, limit)
        ).fetchall()
    # `meters` is a REAL column — without the cast the page reads "210.0m", which is a
    # false precision (the station is not located to the tenth of a metre).
    return [dict(r) | {"meters": int(r["meters"])} for r in rows]
