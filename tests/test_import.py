"""Task 11 — free NYC government supply (Storefront Registry + ACRIS) and CSV import.

Two hard-won field-name corrections, verified live 2026-07-12 against the brief's own
sample code (see docs/implementation-plan.md Task 11 correction for the full story):

  1. `92iy-9c3n` (Storefront Registry) has NO `primary_business_address`,
     `street_number`, or `street_name` columns. The real columns are
     `property_street_address_or` (the full address, pre-joined) and, as a fallback,
     `property_number` + `property_street`. The brief's own code, run unmodified against
     the real response, silently produces addr="" for every row and drops all of them —
     exactly the "field names drifted" failure Task 9 warned about.
  2. `bnx9-e6tj` ("ACRIS - Real Property Master") has NO `borough`/`block`/`lot` columns
     at all — querying it that way is a 400, not an empty result. ACRIS is split across
     datasets: `8h5j-fqxa` ("ACRIS - Real Property Legals") holds the borough/block/lot ->
     document_id join; `bnx9-e6tj` holds document_id -> doc_type/amount/date. A real
     BBL-to-signal lookup needs both, in sequence.

Fixtures below are REAL captured responses (not hand-written), pulled once by hand from
the live endpoints on 2026-07-12: `gov_nyc_storefronts.json` (92iy-9c3n, vacant_on_12_31=
'YES', limit 5), `gov_nyc_acris_legals.json` (8h5j-fqxa, borough=1/block=835/lot=41 — the
Empire State Building BBL, chosen because it actually has ACRIS history), and
`gov_nyc_acris_master.json` (bnx9-e6tj, joined by those document_ids).
"""
import io
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import db
from app.app import app
from app.config import settings
from app.providers import gov_nyc
from app.routes_import import import_csv

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return json.loads((FIX / name).read_text())


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "import.db"))
    db.init_db()


# --- gov_nyc.storefronts() ----------------------------------------------------


def test_storefronts_normalizes_the_real_socrata_response(isolated_db, monkeypatch):
    """Locks in the field-name fix: the real response has no `primary_business_address`,
    `street_number`, or `street_name` — only `property_street_address_or` (and
    `property_number`/`property_street` as a fallback). Against the unfixed brief code
    every one of these 5 real rows would have addr="" and be silently dropped."""
    raw = _fx("gov_nyc_storefronts.json")
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(raw))

    out = gov_nyc.storefronts(limit=5)

    assert len(out) == 5, "all 5 real rows have a usable address + bbl — none may be dropped"
    first = out[0]
    assert first["address"] == "271 BROAD STREET"
    assert first["parcel_id"] == "nyc:5005430010"
    assert first["source_url"] == (
        "https://data.cityofnewyork.us/resource/92iy-9c3n.json?bbl=5005430010"
    )
    assert first["lat"] == pytest.approx(40.6236254)
    assert first["lng"] == pytest.approx(-74.0835487)
    assert first["metro"] == "nyc"
    assert first["status"] == "available"
    assert first["transaction_type"] == "lease"
    assert first["property_type"] == "retail"
    # NYC's own borough names come back ALL CAPS ("STATEN ISLAND"); the rest of the app
    # (metros.yml, seed data, the borough hard-filter) uses Title Case ("Staten Island") —
    # store it normalized, or a `boroughs=["Staten Island"]` search filter can never match.
    assert first["borough"] == "Staten Island"


def test_storefronts_is_a_lead_not_a_listing(isolated_db, monkeypatch):
    """Rule: a vacant storefront has no ask, no size, no broker. None of those may be
    invented, and `our_description` must be OUR sentence, not a copy of the government
    free-text column."""
    raw = _fx("gov_nyc_storefronts.json")
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(raw))

    out = gov_nyc.storefronts(limit=5)

    for rec in out:
        assert "size_sf" not in rec or rec["size_sf"] is None
        assert "asking_rent" not in rec or rec["asking_rent"] is None
        assert "broker_name" not in rec or rec["broker_name"] is None
        assert rec["our_description"] != raw[0].get("primary_business_activity")
        assert "vacancy lead" in rec["our_description"]


def test_storefronts_skips_rows_missing_address_or_bbl(isolated_db, monkeypatch):
    rows = [
        {"bbl": "", "property_street_address_or": "1 NO BBL ST",
         "latitude": "40.7", "longitude": "-74.0"},              # no bbl -> drop
        {"bbl": "1000010001", "property_street_address_or": "",
         "property_number": "", "property_street": "",
         "latitude": "40.7", "longitude": "-74.0"},               # no address -> drop
        {"bbl": "1000010002", "property_number": "42", "property_street": "OK ST",
         "latitude": "40.7", "longitude": "-74.0"},                # fallback address -> keep
    ]
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(rows))

    out = gov_nyc.storefronts(limit=10)

    assert len(out) == 1
    assert out[0]["address"] == "42 OK ST"


def test_storefronts_vacant_only_toggles_the_where_clause(isolated_db, monkeypatch):
    seen = {}

    def _fake_get(url, params=None, timeout=None):
        seen.update(params or {})
        return _FakeResponse([])

    monkeypatch.setattr("httpx.get", _fake_get)

    gov_nyc.storefronts(limit=3, vacant_only=True)
    assert seen["$where"] == "vacant_on_12_31='YES'"

    seen.clear()
    gov_nyc.storefronts(limit=3, vacant_only=False)
    assert seen["$where"] == "1=1"


# --- gov_nyc.acris_signals() --------------------------------------------------


def test_acris_signals_joins_legals_then_master(isolated_db, monkeypatch):
    """The real 2-step join: Legals (borough/block/lot -> document_id), then Master
    (document_id -> doc_type/amount/date). Both real captured responses for the Empire
    State Building's BBL (1008350041)."""
    legals = _fx("gov_nyc_acris_legals.json")
    master = _fx("gov_nyc_acris_master.json")
    calls = []

    def _fake_get(url, params=None, timeout=None):
        calls.append(url)
        if "8h5j-fqxa" in url:
            assert params["borough"] == "1" and params["block"] == 835 and params["lot"] == 41
            return _FakeResponse(legals)
        assert "bnx9-e6tj" in url
        return _FakeResponse(master)

    monkeypatch.setattr("httpx.get", _fake_get)

    out = gov_nyc.acris_signals("1008350041")

    assert calls == [
        "https://data.cityofnewyork.us/resource/8h5j-fqxa.json",
        "https://data.cityofnewyork.us/resource/bnx9-e6tj.json",
    ], "must query Legals before Master -- Master alone has no BBL to filter on"
    assert len(out) == len(master)
    assert out[0] == {
        "doc_type": master[0]["doc_type"],
        "amount": master[0]["document_amt"],
        "date": master[0]["recorded_datetime"],
    }
    # newest first, per the interface contract
    dates = [r["date"] for r in out]
    assert dates == sorted(dates, reverse=True)


def test_acris_signals_no_legals_means_no_master_call(isolated_db, monkeypatch):
    """An empty Legals result is legitimate (not every parcel has ACRIS history) -- and
    it must short-circuit rather than firing a `document_id in()` query at Master, which
    is a malformed SoQL clause, not just a wasted call."""
    calls = []

    def _fake_get(url, params=None, timeout=None):
        calls.append(url)
        return _FakeResponse([])

    monkeypatch.setattr("httpx.get", _fake_get)

    assert gov_nyc.acris_signals("1000000001") == []
    assert len(calls) == 1  # Legals only -- Master was never hit


def test_acris_signals_rejects_a_malformed_bbl(isolated_db, monkeypatch):
    def _must_not_be_called(*a, **kw):
        raise AssertionError("a bbl too short to parse must never reach the network")

    monkeypatch.setattr("httpx.get", _must_not_be_called)
    assert gov_nyc.acris_signals("") == []
    assert gov_nyc.acris_signals("123") == []


# --- routes_import.py: POST /api/import/storefronts ---------------------------


def test_import_storefronts_saves_leads_with_no_invented_data(isolated_db, monkeypatch):
    from app import score

    recs = [
        {"source": "nyc_storefront", "source_url": "https://data.cityofnewyork.us/x?bbl=1",
         "metro": "nyc", "status": "available", "address": "1 Vacant Ave",
         "borough": "Staten Island", "lat": 40.1, "lng": -74.1, "property_type": "retail",
         "transaction_type": "lease", "parcel_id": "nyc:1",
         "our_description": "Vacant ground-floor commercial space at 1 Vacant Ave."},
        {"source": "nyc_storefront", "source_url": "https://data.cityofnewyork.us/x?bbl=2",
         "metro": "nyc", "status": "available", "address": "2 Vacant Ave",
         "borough": "Staten Island", "lat": None, "lng": None, "property_type": "retail",
         "transaction_type": "lease", "parcel_id": "nyc:2",
         "our_description": "Vacant ground-floor commercial space at 2 Vacant Ave."},
    ]
    monkeypatch.setattr(gov_nyc, "storefronts", lambda limit=500: recs)
    enrich_calls = []
    monkeypatch.setattr(score, "enrich", lambda lid: enrich_calls.append(lid))

    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        r = c.post("/api/import/storefronts")
        assert r.status_code == 200, r.text
        assert r.json() == {"fetched": 2, "saved": 2}

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM listing WHERE address='1 Vacant Ave'").fetchone()
    assert row["source"] == "nyc_storefront"
    assert row["size_sf"] is None       # a lead, not a listing -- never a fake 0
    assert row["asking_rent"] is None
    assert row["broker_name"] is None
    assert row["status"] == "available"
    # only the row WITH lat/lng triggers enrichment -- the other has no coordinates yet
    assert enrich_calls == [row["id"]]


def test_import_storefronts_requires_auth():
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/api/import/storefronts").status_code != 200


# --- routes_import.py: POST /api/import/csv -----------------------------------


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


def test_csv_import_requires_auth():
    with TestClient(app, follow_redirects=False) as c:
        csv_bytes = b"address\n1 X St\n"
        r = c.post("/api/import/csv?metro=nyc",
                   files={"file": ("f.csv", io.BytesIO(csv_bytes), "text/csv")})
        assert r.status_code != 200


def test_csv_import_rejects_unmappable_columns(isolated_db):
    """A file whose columns can't be mapped to any Listing field must import NOTHING --
    not silently create rows with a synthetic address and every real field null."""
    rows = [{"foo": "bar", "baz": "qux"}]
    assert import_csv(rows, "nyc") == 0
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM listing").fetchone()["c"] == 0


def test_csv_import_ignores_unrecognized_columns_but_keeps_mapped_ones(isolated_db):
    """Forgiving, not lying: an unmapped column (e.g. a broker's internal 'notes' field)
    is silently dropped; every column we DO recognize still lands."""
    rows = [{"address": "9 Mixed St", "notes": "internal only, ignore me",
             "broker": "Jane Broker", "sf": "2200"}]
    assert import_csv(rows, "nyc") == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM listing WHERE address='9 Mixed St'").fetchone()
    assert row["broker_name"] == "Jane Broker"
    assert row["size_sf"] == 2200


def test_csv_import_parses_dollar_and_comma_formatted_numbers(isolated_db):
    rows = [{"address": "5 Formatted Rd", "size_sf": "3,200", "asking_rent": "$45.50"}]
    assert import_csv(rows, "nyc") == 1
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM listing WHERE address='5 Formatted Rd'").fetchone()
    assert row["size_sf"] == 3200
    assert row["asking_rent"] == 45.5


def test_csv_import_route_rejects_unknown_metro(isolated_db):
    with TestClient(app, follow_redirects=False) as c:
        c.post("/login", data={"password": "test-pw"})
        csv_bytes = b"address\n1 X St\n"
        r = c.post("/api/import/csv?metro=not-a-metro",
                   files={"file": ("f.csv", io.BytesIO(csv_bytes), "text/csv")})
        assert r.status_code == 200
        assert "error" in r.json()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM listing").fetchone()["c"] == 0
