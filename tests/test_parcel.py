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
