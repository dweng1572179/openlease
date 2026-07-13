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
    db.save_listing(dict(source="test", metro="mia", source_url="t://noask", address="9 No Ask Ave",
                         property_type="retail", size_sf=1500, neighborhood="Wynwood"))
    assert [r["source_url"] for r in db.filter_listings(q, "mia")] == ["t://noask"]


def test_rent_unit_normalization(client):
    # $6/SF/MO is $72/SF/YR — it must fail a $64 cap, not pass it
    db.save_listing(dict(source="test", metro="chi", source_url="t://permo", address="1 Monthly St",
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


def test_near_miss_reply_names_relaxed_constraint_exactly_once(client):
    # The near-miss disclosure must be composed in exactly ONE place. Before the fix, the
    # phrase was said once by routes_search.py's prepend AND once by ai.reply()'s own
    # keyless text, so `reply` (part of the JSON API contract) doubled the sentence even
    # before the HTML banner added a third telling. Count occurrences directly against
    # the wire-contract `reply` field, not the HTML.
    r = client.post("/api/search", json={
        "message": "office around 8000 sf under $1/sf", "metro": "nyc"})
    body = r.json()
    assert body["isNearMiss"] is True, body["reply"]
    reply_lower = body["reply"].lower()
    assert reply_lower.count("nothing matched exactly") == 1, body["reply"]
    assert "rent cap" in reply_lower, body["reply"]   # still names what was relaxed


def test_metro_switch_drops_stale_geographic_constraint(client):
    # A stale priorState carrying a Miami bbox (as a keyed neighborhood search would set)
    # must not poison a follow-up scoped to a different metro. Without the fix, NYC
    # coordinates never fall inside Miami's bbox, filter_listings returns zero rows on
    # every subsequent turn, and even the near-miss ladder can't rescue it -- _relax's
    # "neighborhood" stage clears neighborhood/boroughs but never the bbox.
    stale_prior = {
        "propertyTypes": [], "transactionType": "lease", "boroughs": [],
        "neighborhood": "Wynwood", "minSizeSf": 0, "maxSizeSf": 0, "maxRentPerSfYr": 0,
        "minLat": 25.7, "maxLat": 25.8, "minLng": -80.2, "maxLng": -80.1,
        "excludeAddrStates": [], "excludeZip3": [], "excludeCities": [], "keywords": [],
    }
    r = client.post("/api/search", json={
        "message": "office space", "metro": "nyc", "priorState": stale_prior})
    body = r.json()
    assert body["results"], "a metro switch must not carry Miami's bbox into NYC forever"
    assert body["query"]["mustHaves"]["minLat"] == 0, "the foreign bbox must be dropped"


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


def test_suggestion_chips_are_not_broken_html(client):
    """`{{ s|tojson }}` in an onclick emits a DOUBLE-quoted JSON string and does not escape
    `"`, so the first quote TERMINATED the onclick attribute and the rest of the handler was
    parsed as junk boolean attributes. Every suggestion chip was a dead no-op — and
    follow-up refinement is a headline feature. (`forceescape` fixes it; the pins island in
    the same template already did this correctly.)"""
    from html.parser import HTMLParser

    r = client.post("/search", data={"message": "office around 8000 sf under $1/sf",
                                     "metro": "nyc"})
    assert r.status_code == 200
    assert "htmx.trigger" in r.text, "the suggestion chips should be on the page at all"

    handlers = []

    class _P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag != "button" or "data-suggestion" not in d:
                return
            if "onclick" in d:
                handlers.append(d["onclick"])
            # a truncated attribute leaks the rest of the handler as bare attributes
            # ("show", "only", "ground", 'floor";') — that is the bug this test exists for
            assert not any(v is None and k != "data-suggestion" for k, v in attrs), attrs

    _P().feed(r.text)
    assert handlers, "no onclick survived the parse — the attribute was truncated"
    for h in handlers:
        assert h.rstrip().endswith("'submit')"), h   # the WHOLE handler is intact
        assert "htmx.trigger" in h


def test_a_drawn_area_is_a_HARD_constraint_not_a_re_ranking(client):
    """SpaceFinder's "Draw area". A rectangle the user drew is a bbox, and a bbox is a
    constraint — a listing outside it must not appear because it ranked well. It also
    overrides whatever bbox the message implied: the user drew it, so they mean it."""
    # a box tight around Wynwood
    wynwood = "25.79,-80.21,25.81,-80.19"
    r = client.post("/api/search", json={"message": "retail", "metro": "mia", "bbox": wynwood})
    body = r.json()
    assert body["results"], body["reply"]
    for x in body["results"]:
        assert 25.79 <= x["lat"] <= 25.81, x["address"]
        assert -80.21 <= x["lng"] <= -80.19, x["address"]

    # a box over open water off Miami — nothing is in it, and we say so rather than
    # quietly handing back the nearest thing
    ocean = "25.60,-80.00,25.62,-79.98"
    body = client.post("/api/search",
                       json={"message": "retail", "metro": "mia", "bbox": ocean}).json()
    assert body["results"] == []


def test_an_unparseable_drawn_bbox_is_ignored_not_crashed_on(client):
    body = client.post("/api/search",
                       json={"message": "retail", "metro": "mia", "bbox": "garbage"}).json()
    assert body["results"]          # falls back to the query's own geography


def test_saved_only_filters_to_the_shortlist(client):
    from app import db
    all_hits = client.post("/api/search", json={"message": "retail", "metro": "mia"}).json()
    assert all_hits["results"]
    lid = all_hits["results"][0]["id"]

    none_saved = client.post("/api/search",
                             json={"message": "retail", "metro": "mia", "savedOnly": True}).json()
    assert none_saved["results"] == [] or all(
        x["id"] != lid for x in none_saved["results"])

    if not db.is_saved(lid):
        db.toggle_save(lid)
    only = client.post("/api/search",
                       json={"message": "retail", "metro": "mia", "savedOnly": True}).json()
    assert [x["id"] for x in only["results"]] == [lid]
    db.toggle_save(lid)          # leave the shared DB as we found it


def test_address_lookup_never_guesses(client, monkeypatch):
    """The map's "Look up an address". A metro-scoped geocoder will hand back a same-named
    street in its own city rather than decline — so a miss must return nulls WITH a reason,
    not a plausible pin somewhere else."""
    from app import crawl
    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: None)
    r = client.post("/api/geocode", json={"address": "nowhere at all", "metro": "nyc"})
    body = r.json()
    assert body["lat"] is None and body["lng"] is None
    assert "no match" in body["reason"]

    monkeypatch.setattr(crawl, "_geocode", lambda addr, metro: (40.7484, -73.9857))
    body = client.post("/api/geocode",
                       json={"address": "350 5th Ave", "metro": "nyc"}).json()
    assert body["lat"] == 40.7484 and body["lng"] == -73.9857


def test_recent_threads_are_listed_and_resumable(client):
    """SpaceFinder's "Recent" + "New chat". /api/sessions has existed since Task 5 with no
    UI on it. A follow-up REFINES the prior turn, so resuming an old thread has to replay
    that turn's mustHaves as priorState — otherwise "Recent" is just a list of strings."""
    r1 = client.post("/api/search", json={"message": "retail in wynwood 1500 sf",
                                          "metro": "mia"}).json()
    sid = r1["sessionId"]
    client.post("/api/search", json={"message": "make it bigger — at least 5000 sf",
                                     "metro": "mia", "sessionId": sid,
                                     "priorState": r1["query"]["mustHaves"]})

    sessions = client.get("/api/sessions").json()["sessions"]
    me = next(s for s in sessions if s["id"] == sid)
    assert me["turns"] == 2 and me["metro"] == "mia"
    assert me["title"]                                   # something to click on

    # resuming hands back the last turn's mustHaves — that IS the refinement state
    turns = client.get(f"/api/sessions/{sid}").json()["turns"]
    assert turns[-1]["mustHaves"]["minSizeSf"] == 5000
    assert turns[-1]["mustHaves"]["propertyTypes"] == ["retail"]   # carried through the thread


def test_the_home_page_has_the_recent_and_new_chat_controls(client):
    html = client.get("/").text
    assert 'id="new_chat"' in html and 'id="recent_btn"' in html
    assert 'id="draw"' in html and 'id="addr"' in html and 'id="saved_chk"' in html
