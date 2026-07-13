"""geosearch.py: NYC's free, keyless geocoder, and the only way to get a BBL (the PLUTO
parcel join key) without a key.

Run: `python -m pytest tests/test_geosearch.py -v` from openlease/.
"""
import pytest

from app import db
from app.config import settings
from app.providers import geosearch


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "geosearch.db"))
    db.init_db()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_ESB_FEATURE = {
    "features": [{
        "geometry": {"coordinates": [-73.985656, 40.748441]},
        "properties": {
            "label": "350 5 AVENUE, New York, NY, USA",
            "borough": "Manhattan",
            "addendum": {"pad": {"bbl": "1008350041", "bin": "1015862"}},
        },
    }]
}


def test_geocode_extracts_lat_lng_bbl_borough_matched(isolated_db, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(_ESB_FEATURE))

    result = geosearch.geocode("350 5th Ave, New York, NY")

    assert result == {
        "lat": 40.748441, "lng": -73.985656, "bbl": "1008350041",
        "borough": "Manhattan", "matched": "350 5 AVENUE, New York, NY, USA",
    }


def test_geocode_returns_none_for_no_matches(isolated_db, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse({"features": []}))

    assert geosearch.geocode("not a real address at all") is None


def test_geocode_bbl_is_none_not_zero_when_addendum_missing(isolated_db, monkeypatch):
    """A feature with no PAD addendum (rare, but real for some non-taxlot points) must come
    back with bbl=None — never a fabricated 0 or empty string."""
    feature = {
        "features": [{
            "geometry": {"coordinates": [-73.9, 40.7]},
            "properties": {"label": "Somewhere, NY", "borough": "Queens"},
        }]
    }
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(feature))

    result = geosearch.geocode("somewhere")

    assert result["bbl"] is None
    assert result["borough"] == "Queens"


def test_request_identifies_itself_with_the_crawl_user_agent(isolated_db, monkeypatch):
    seen = {}

    def _fake_get(url, params=None, headers=None, timeout=None):
        seen["headers"] = headers
        return _FakeResponse(_ESB_FEATURE)

    monkeypatch.setattr("httpx.get", _fake_get)
    geosearch.geocode("350 5th Ave")

    assert seen["headers"]["User-Agent"] == settings.crawl_user_agent


def test_a_wrong_street_is_rejected_not_returned(isolated_db, monkeypatch):
    """GeoSearch only covers the five boroughs and it does NOT decline. Asked for
    "205 Hallock Road, Stony Brook NY" it returns "205 DAHILL ROAD, Brooklyn" — a different
    street, in a different place — with match_type "fallback" and confidence 0.8, the SAME
    values it reports for a correct hit. So its own confidence signal cannot separate them.

    Crawling a national feed under `nyc`, that meant every Long Island and out-of-state
    address got silently pinned somewhere in Brooklyn and handed a New York Walk Score. We
    check the one thing a geocoder cannot fudge: the street we asked for has to appear in
    the address we got back. No match is None — never a confident wrong answer."""
    wrong = {"features": [{
        "geometry": {"coordinates": [-73.9803, 40.6421]},
        "properties": {"label": "205 DAHILL ROAD, Brooklyn, NY, USA", "borough": "Brooklyn",
                       "match_type": "fallback", "confidence": 0.8, "addendum": {}},
    }]}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(wrong))
    assert geosearch.geocode("205 hallock road stony brook ny") is None


def test_an_ordinal_street_still_matches(isolated_db, monkeypatch):
    """We ask for "350 5th Ave"; GeoSearch answers "350 5 AVENUE". Same street."""
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(_ESB_FEATURE))
    got = geosearch.geocode("350 5th Ave, New York, NY")
    assert got and got["matched"] == "350 5 AVENUE, New York, NY, USA"


def test_census_rejects_a_wrong_street_too(isolated_db, monkeypatch):
    """Same standard as every other geocoder here: the street we asked for has to be the
    street we got back."""
    from app.providers import census
    payload = {"result": {"addressMatches": [{
        "matchedAddress": "205 DAHILL ROAD, BROOKLYN, NY, 11218",
        "coordinates": {"x": -73.9803, "y": 40.6421}}]}}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(payload))
    assert census.geocode("205 Hallock Road, Stony Brook, NY") is None

    ok = {"result": {"addressMatches": [{
        "matchedAddress": "540 ROSE AVE, VENICE, CA, 90291",
        "coordinates": {"x": -118.4729, "y": 33.9986}}]}}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(ok))
    got = census.geocode("540 Rose Avenue, Venice, CA")
    assert got and got["lat"] == 33.9986
