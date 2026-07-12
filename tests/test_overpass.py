"""overpass.py's two hard-won rules (spec Section 2, constraints.md):

1. An EMPTY response is an ERROR, never a score of 0 — `overpass.osm.ch` is a
   Switzerland-only extract that returns HTTP 200 + zero elements for US coordinates,
   which would otherwise silently score every American listing 0.
2. Only the allowlisted mirrors are ever contacted — checked BEFORE any network call,
   proven here by making the fetch function raise if it's ever invoked.

Also locks in a real bug found live: `overpass-api.de` returns 406 Not Acceptable to
httpx's default `python-httpx/x.y.z` User-Agent. Every request must identify itself with
`settings.crawl_user_agent`.

Run: `python -m pytest tests/test_overpass.py -v` from openlease/.
"""
import pytest

from app import db
from app.config import settings
from app.providers import overpass


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "overpass.db"))
    db.init_db()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_disallowed_host_raises_before_any_network_call(monkeypatch):
    monkeypatch.setattr(settings, "overpass_url", "https://overpass.osm.ch/api/interpreter")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("httpx.post must never be called for a non-allowlisted host")

    monkeypatch.setattr("httpx.post", _must_not_be_called)

    with pytest.raises(RuntimeError, match="overpass.osm.ch"):
        overpass.pois(40.7484, -73.9857)


def test_allowlisted_hosts_are_exactly_the_two_documented_mirrors():
    assert overpass.ALLOWED_HOSTS == ("overpass-api.de", "overpass.kumi.systems")


def test_empty_elements_raises_overpass_empty_not_a_zero_score(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "overpass_url", "https://overpass-api.de/api/interpreter")
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse({"elements": []}))

    with pytest.raises(overpass.OverpassEmpty):
        overpass.pois(34.0033, -118.2100)


def test_request_identifies_itself_with_the_crawl_user_agent(isolated_db, monkeypatch):
    """Locks in the fix for the real 406: overpass-api.de rejects httpx's default UA."""
    monkeypatch.setattr(settings, "overpass_url", "https://overpass-api.de/api/interpreter")
    seen_headers = {}

    def _fake_post(url, data=None, headers=None, timeout=None):
        seen_headers.update(headers or {})
        return _FakeResponse({"elements": [{"type": "node", "lat": 1.0, "lon": 2.0,
                                             "tags": {"amenity": "cafe"}}]})

    monkeypatch.setattr("httpx.post", _fake_post)
    overpass.pois(40.0, -73.0)

    assert seen_headers.get("User-Agent") == settings.crawl_user_agent


def test_query_uses_nwr_not_node_and_requests_a_center_point():
    q = overpass._query(40.7484, -73.9857)
    assert "nwr(" in q
    assert "node(" not in q
    assert "out center tags" in q


def test_normalize_handles_nodes_ways_and_bus_stops(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "overpass_url", "https://overpass-api.de/api/interpreter")
    elements = [
        # a node: lat/lon directly on the element
        {"type": "node", "lat": 40.75, "lon": -73.98,
         "tags": {"shop": "supermarket", "name": "Whole Foods"}},
        # a way: no lat/lon, only a `center` (this is exactly what `out center tags` gives us,
        # and exactly what a node-only query would have missed)
        {"type": "way", "center": {"lat": 40.76, "lon": -73.97},
         "tags": {"leisure": "park", "name": "Central Park"}},
        # a bus stop: route_ref is semicolon-separated
        {"type": "node", "lat": 40.70, "lon": -73.99,
         "tags": {"highway": "bus_stop", "name": "5th Ave/34th St", "route_ref": "B1;B4; "}},
        # an element with tags that match nothing we care about — must be dropped, not crash
        {"type": "node", "lat": 40.71, "lon": -73.90, "tags": {"amenity": "parking"}},
        # an element with no coordinates at all (malformed) — must be dropped, not crash
        {"type": "way", "tags": {"amenity": "cafe"}},
    ]
    monkeypatch.setattr("httpx.post", lambda *a, **kw: _FakeResponse({"elements": elements}))

    result = overpass.pois(40.75, -73.98)

    by_cat = {r["category"]: r for r in result if r["category"] != "bus_stop"}
    assert by_cat["grocery"] == {"category": "grocery", "name": "Whole Foods",
                                  "lat": 40.75, "lng": -73.98, "route_refs": []}
    assert by_cat["parks"] == {"category": "parks", "name": "Central Park",
                                "lat": 40.76, "lng": -73.97, "route_refs": []}
    bus = next(r for r in result if r["category"] == "bus_stop")
    assert bus["route_refs"] == ["B1", "B4"]
    # the parking node (unmatched tags) and the coordinate-less way both vanish silently --
    # exactly two categorized POIs plus the one bus stop, nothing else
    assert len(result) == 3
