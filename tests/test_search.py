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


def test_near_miss_discloses_every_constraint_it_dropped(client):
    # The ladder relaxes CUMULATIVELY. Here the rent cap alone is not enough to surface
    # anything, so the size stage widens on top of an already-dropped rent cap. Reporting
    # only the last stage ("the size range") would tell the user their $1/SF ceiling still
    # held while handing them a $95/SF listing. Every constraint actually in force must be
    # named. Without the fix this returns "the size range" and the assertion below fails.
    r = client.post("/api/search", json={
        "message": "retail around 1000 sf under $1/sf", "metro": "mia"})
    body = r.json()
    assert body["isNearMiss"] is True, body["reply"]
    assert body["results"], "relaxation should have found the near misses"

    relaxed = body["query"]["relaxed"]
    assert "rent cap" in relaxed, relaxed          # the dropped one it used to hide
    assert "size range" in relaxed, relaxed

    # ...and the listing we hand back really does violate the stated cap, which is exactly
    # why the disclosure has to be complete.
    asked = body["query"]["mustHaves"]["maxRentPerSfYr"]
    assert any(x["askingRent"] > asked for x in body["results"] if x["rentUnit"] == "sf_yr")


def test_near_miss_ladder_terminates_when_nothing_helps(client):
    # "land" has zero listings in any metro at any size — the SIZE tier of the softness
    # ladder must not loop forever re-widening itself (it grows a MAX bound, which never
    # zeroes out, so a naive "relax while still set" check re-enters the same tier and
    # never reaches the next one — that's an infinite widen that overflows SQLite's
    # INTEGER bind). The ladder must exhaust all tiers once each and give up honestly.
    r = client.post("/api/search", json={
        "message": "land in miami around 1500 sf", "metro": "mia"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] == []
    assert body["isNearMiss"] is False


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

    # ...and the single-session view round-trips both turns, in order, with their mustHaves
    turns = client.get(f"/api/sessions/{sid}").json()["turns"]
    assert [t["message"] for t in turns] == [
        "retail in wynwood 1500 sf", "make it bigger — at least 5000 sf"]
    assert turns[1]["mustHaves"]["minSizeSf"] == 5000
