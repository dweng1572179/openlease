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


def test_search_ui_and_listing_page(monkeypatch):
    from app import db, registry, seed
    # T9 wired a LIVE, request-time parcel lookup into /listings/{id} for any metro with a
    # real ParcelProvider (every metro, as of T9). This test's seed listing has no
    # parcel_id yet, so without this guard it would fire a real network call at the
    # Miami-Dade PA ArcGIS endpoint on every run of the suite -- exactly what "hermetic"
    # forbids. Parcel behavior itself is covered end-to-end by tests/test_parcel.py.
    monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)
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


def test_listing_page_links_real_broker_source_url(monkeypatch):
    # The spec's "always link sourceUrl" rule, for the one case that matters for a live
    # crawl: a real http(s) source_url. The link must be rendered AND the footnote must
    # point at it -- unlike the seed:// case above, where both are correctly absent.
    from app import db, registry
    monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)  # see T9 note above
    with TestClient(app, follow_redirects=False) as c:
        db.init_db()
        c.post("/login", data={"password": "test-pw"})
        lid = db.save_listing(dict(source="test", 
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


# --- Task 13: workspace (saves, portfolios, export, AI highlights, per-listing chat) ------

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


def test_portfolio_add_and_scoped_export():
    """The brief's routes_portfolios.py wires up POST /portfolios/{id}/add but the given
    template snippets never call it end-to-end -- exercise the full path: create two
    portfolios, add a listing to only one, and confirm a portfolio-scoped export contains
    it while the OTHER portfolio's export does not (no cross-portfolio leakage)."""
    from app import db, seed
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://mia/1'").fetchone()["id"]

        c.post("/portfolios", data={"name": "Wynwood shortlist"})
        pid = db.list_portfolios()[0]["id"]
        r = c.post(f"/portfolios/{pid}/add", data={"listing_id": lid})
        # the route returns the button's server-rendered replacement, not JSON: the UI
        # must never claim a success the server didn't actually perform
        assert r.status_code == 200 and "added" in r.text

        items = db.portfolio_items(pid)
        assert len(items) == 1 and items[0]["id"] == lid

        r = c.get(f"/export.csv?portfolio_id={pid}")
        assert r.status_code == 200 and "2618 NW 2nd Ave" in r.text

        pid2 = db.create_portfolio("Empty shortlist")
        r = c.get(f"/export.csv?portfolio_id={pid2}")
        assert r.status_code == 200 and "2618 NW 2nd Ave" not in r.text


def test_saved_listing_card_never_prints_python_none_for_missing_rationale():
    """`_listing_card.html`'s rationale line is populated per-QUERY by rank.py and is
    never persisted on the row itself -- a listing reached via db.list_saved() (no ranking
    pass) has rationale=NULL. A bare `{{ l.rationale }}` renders a Python None as the
    literal text 'None' (str(None) -- Jinja does not blank out a defined-but-None value),
    which would show up under every card on the new saved/portfolio page. Regression guard."""
    from app import db, seed
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://nyc/1'").fetchone()["id"]
        assert db.get_listing(lid)["rationale"] is None     # never computed for this row

        if not db.is_saved(lid):        # tests share one on-disk DB -- force a known state
            c.post(f"/listings/{lid}/save")
        assert db.is_saved(lid) is True
        r = c.get("/portfolios")
        assert r.status_code == 200 and "55 Gansevoort St" in r.text
        assert ">None<" not in r.text


def test_listing_page_shows_highlights_gate_and_saved_state(monkeypatch):
    """Keyless: highlights are silently absent (never a crash, never invented text) and
    the Save button reflects true saved state; the chat panel still renders its form."""
    from app import db, registry, seed
    monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)  # see T6 note
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://nyc/2'").fetchone()["id"]
        if db.is_saved(lid):             # tests share one on-disk DB -- force a known state
            db.toggle_save(lid)
        assert db.is_saved(lid) is False

        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert 'name="question"' in r.text                  # the chat form is present
        assert db.get_listing(lid)["highlights_json"] is None  # no key -> never generated
        assert "Save</button>" in r.text                     # not yet saved
        assert "Saved</button>" not in r.text

        # ...and the gate is actually SHOWN. The keyless promise is that an LLM feature
        # degrades HONESTLY and LOUDLY — not that it silently renders nothing. Without
        # this the `{% elif not ai_available %}` branch could vanish and every assertion
        # above would still pass, leaving the user staring at an empty panel with no idea
        # a key would fill it.
        assert "ANTHROPIC_API_KEY" in r.text, "the keyless gate must say what's missing"
        assert "/settings" in r.text, "...and where to fix it"

        c.post(f"/listings/{lid}/save")
        r = c.get(f"/listings/{lid}")
        assert "Saved</button>" in r.text


def test_a_failed_portfolio_add_says_so_instead_of_claiming_success(monkeypatch):
    """The button used to reveal a hidden 'added' label from an onclick that fired the
    moment you clicked it — so a failed add still told the user it had worked. The server
    now renders the outcome and HTMX swaps it in, so the UI cannot claim a success it
    didn't get."""
    from app import db, registry, seed
    monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)
    with TestClient(app, follow_redirects=False) as c:
        seed.seed()
        c.post("/login", data={"password": "test-pw"})
        with db.get_conn() as conn:
            lid = conn.execute(
                "SELECT id FROM listing WHERE source_url='seed://nyc/1'").fetchone()["id"]

        pid = db.create_portfolio("Acme Corp")
        ok = c.post(f"/portfolios/{pid}/add", data={"listing_id": lid})
        assert ok.status_code == 200 and "added" in ok.text

        # a portfolio that does not exist -> FK violation -> must NOT report success
        bad = c.post("/portfolios/999999/add", data={"listing_id": lid})
        assert bad.status_code == 500, bad.text
        assert "added" not in bad.text
        assert "couldn't add" in bad.text


def test_the_listing_page_shows_the_evidence_behind_the_scores(monkeypatch):
    """SpaceFinder puts the POIs, the nearest transit and the airport drive times on every
    listing page — that enrichment IS the moat. We were computing all of it at ingest,
    storing it in `poi` and `transit_nearby`, and then never reading either table. A Walk
    Score with nothing behind it is an assertion; this is the evidence for it."""
    from app import db, registry
    from app.providers import osrm
    monkeypatch.setattr(registry, "parcel_provider", lambda metro: None)
    monkeypatch.setattr(osrm, "drive_minutes", lambda lat, lng, metro: {"JFK": 31.0, "LGA": 18.0})

    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        lid = db.save_listing(dict(source="test", source_url="t://evidence", metro="nyc",
                                   address="350 5th Ave", lat=40.7484, lng=-73.9857,
                                   walk_score=100, transit_score=100))
        with db.get_conn() as conn:
            conn.execute("INSERT INTO poi (listing_id, category, name, lat, lng, meters) "
                         "VALUES (?,?,?,?,?,?)", (lid, "coffee", "Blue Bottle", 40.75, -73.98, 120))
            conn.execute("INSERT INTO transit_nearby (listing_id, mode, route, name, meters) "
                         "VALUES (?,?,?,?,?)", (lid, "rail", "B,D,F,M", "34 St-Herald Sq", 210))

        r = c.get(f"/listings/{lid}")
        assert r.status_code == 200
        assert "Blue Bottle" in r.text and "120m" in r.text        # the POIs behind the score
        assert "34 St-Herald Sq" in r.text and "210m" in r.text    # the stations behind it
        assert "JFK" in r.text and "31 min" in r.text              # airport drive times
        assert "no traffic" in r.text.lower()   # OSRM is free-flow — say so, don't imply live


def test_the_switcher_does_not_offer_an_empty_market():
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        html = c.get("/").text
        assert 'value="nyc"' in html and 'value="mia"' in html and 'value="la"' in html
        assert 'value="chi"' not in html, "Chicago has no crawlable supply — don't offer it"
