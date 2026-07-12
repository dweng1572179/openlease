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
from app.config import settings  # noqa: E402
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


def test_a_published_field_is_never_silently_dropped():
    """Socrata serializes numerics as decimal STRINGS, inconsistently: PLUTO gives
    numfloors as "102.0000000" but yearbuilt as "1931". A bare `int()` cast raises on the
    former, the helper swallowed it, and `floors` came back None — which the listing page
    renders as "not published in this market", for a field NYC publishes on every lot.

    That is a WRONG answer wearing a null's clothes, and it is exactly what this module
    exists to prevent. `missing_reason` is what makes a null honest; a null with no reason
    on a field the market DOES publish is a bug, not an admission.

    The fixture is the Empire State Building: 102 floors, built 1931."""
    p = parcel_nyc.normalize(_fx("nyc"))
    assert p.floors == 102, p.floors           # int("102.0000000") raises -> was None
    assert p.year_built == 1931
    assert p.units and p.bldg_sqft
    # every null this provider returns must be explained; NYC explains none because it
    # publishes all of them.
    for field in ("floors", "year_built", "lot_sqft", "bldg_sqft", "units"):
        assert getattr(p, field) is not None, f"{field} is published by NYC — a null here is a bug"


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


# --- Fix 7 (review pass): crawl-time geocoding via each metro's existing free provider ---
# No rung of extract.py resolves an address to lat/lng. These `geocode()` functions (new,
# added alongside crawl.py's `_geocode()` wiring) are what crawl.py calls at ingest so a
# real crawl produces listings that actually land on the map. No new dependency, no new
# key -- reuses the SAME ArcGIS/Socrata endpoints `lookup()` already queries, just asking
# for point geometry instead of attributes-only.
#
# Each `geocode()` call goes through `cache.cached()` (same as `lookup()`), which needs
# the `provider_cache` table -- isolated per test (own tmp_path db), independent of this
# file's own module-level DB_PATH quirk above (which only matters for the pre-existing
# `.normalize()`/round-trip tests, none of which touch the cache).

@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "parcel_geocode.db"))
    db.init_db()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_miami_geocode_reads_point_geometry_directly(monkeypatch, isolated_db):
    """Miami's PA layer is a POINT layer (verified live) -- outSR=4326 hands back
    WGS84 lat/lng straight off geometry.y/geometry.x, no centroid math needed."""
    payload = {"features": [{"attributes": {"FOLIO": "0101100501140"},
                             "geometry": {"x": -80.192, "y": 25.775}}]}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(payload))

    result = parcel_miami.geocode("100 NE 1 Ave, Miami, FL")

    assert result == {"lat": 25.775, "lng": -80.192}


def test_miami_geocode_returns_none_for_no_match(monkeypatch, isolated_db):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse({"features": []}))
    assert parcel_miami.geocode("not a real address") is None


def test_la_geocode_computes_a_polygon_centroid(monkeypatch, isolated_db):
    """LA parcels are POLYGONS (verified live), and this server does not support
    returnCentroid (verified live -- no `centroid` key ever comes back), so the centroid
    is computed from the ring here. A simple square [(0,0),(2,0),(2,2),(0,2),(0,0)] in
    (lng,lat) order has an unambiguous centroid at (lng=1, lat=1)."""
    payload = {"features": [{"attributes": {"AIN": "123"},
                             "geometry": {"rings": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}}]}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse(payload))

    result = parcel_la.geocode("200 N Spring St, Los Angeles, CA")

    assert result is not None
    assert result["lat"] == pytest.approx(1.0)
    assert result["lng"] == pytest.approx(1.0)


def test_la_geocode_returns_none_for_a_degenerate_or_missing_ring(monkeypatch, isolated_db):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse({"features": [
        {"attributes": {"AIN": "1"}, "geometry": {"rings": []}}]}))
    assert parcel_la.geocode("nowhere") is None


def test_chicago_geocode_chains_address_to_pin_to_lat_lon(monkeypatch, isolated_db):
    """Reuses the SAME address->PIN dataset `lookup()` uses, then a second Socrata call
    to the Parcel Universe dataset -- the one Cook County dataset that actually carries
    lat/lon per PIN (ADDR and ATTRS, which `lookup()` also queries, do not)."""
    calls = []

    def _fake_get(url, params=None, timeout=None):
        calls.append(url)
        if url == parcel_chicago.ADDR:
            return _FakeResponse([{"pin": "20044100320000", "pin10": "2004410032"}])
        if url == parcel_chicago.UNIVERSE:
            return _FakeResponse([{"pin": "20044100320000", "lat": "41.8152538744",
                                    "lon": "-87.6301751085"}])
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("httpx.get", _fake_get)

    result = parcel_chicago.geocode("4326 S LaSalle St, Chicago, IL")

    assert result == {"lat": 41.8152538744, "lng": -87.6301751085}
    assert parcel_chicago.ADDR in calls and parcel_chicago.UNIVERSE in calls


def test_chicago_geocode_returns_none_when_the_address_has_no_pin(monkeypatch, isolated_db):
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse([]))
    assert parcel_chicago.geocode("nowhere real") is None


def test_no_metro_geocode_ever_fakes_0_0_on_failure(monkeypatch, isolated_db):
    """constraints.md: `None != 0 != "lookup failed"`, and 0,0 is the Gulf of Guinea --
    every geocode() must return None (never a fabricated origin point) on a miss."""
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse({"features": []}))
    assert parcel_miami.geocode("x") is None
    assert parcel_la.geocode("x") is None
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse([]))
    assert parcel_chicago.geocode("x") is None
