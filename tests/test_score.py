"""Walk Score against Walk Score's OWN published values (ESB=100, Bay Ridge=98), off the
committed Overpass fixtures — fast, offline, deterministic. The decay curve is checked
against its three published anchors directly."""
import json
import os
import pathlib
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "openlease_score.db")
os.environ["DB_PATH"] = _DB
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except FileNotFoundError:
        pass

import pytest  # noqa: E402

from app import score  # noqa: E402
from app.providers import rail  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name: str) -> dict:
    p = FIX / f"overpass_{name}.json"
    if not p.exists():
        pytest.skip(f"{p.name} not captured — see Task 7 Step 8")
    return json.loads(p.read_text())


def test_decay_matches_published_anchors():
    assert score.decay(0) == 1.0
    assert score.decay(402) == 1.0          # full credit inside a quarter mile
    assert score.decay(2414) == 0.0         # zero credit at 1.5 miles
    assert score.decay(3000) == 0.0
    assert score.decay(500) > score.decay(1500) > score.decay(2400) > 0.0


def test_weights_sum_to_fifteen_and_normalize_to_100():
    assert abs(score.MAX_WEIGHT - 15.0) < 0.05, score.MAX_WEIGHT
    assert round(score.MAX_WEIGHT * score.MULTIPLIER) == 100


def test_empire_state_building_scores_100():
    f = _fx("esb")
    s, breakdown = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert s >= 98, (s, {k: v["score"] for k, v in breakdown.items()})
    assert breakdown["grocery"]["nearest_m"] is not None
    assert breakdown["restaurants"]["count"] > 20


def test_bay_ridge_scores_high_but_below_midtown():
    f = _fx("bay_ridge")
    s, _ = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert 93 <= s <= 100, s      # Walk Score publishes 98
    esb = _fx("esb")
    assert s <= score.walk_score(esb["lat"], esb["lng"], esb["pois"])[0]


def test_industrial_district_scores_low():
    """The control: if a Vernon industrial block also scored ~100, the score would be
    measuring nothing. This is the ONLY test that proves the score discriminates, so its
    bound has to be tight enough to fail a broken score. Vernon really scores 18 against
    the committed fixture; `< 75` would wave through a score that had gone uniformly high
    (a 74 here means the metric is nearly dead) and the guard would be worthless."""
    f = _fx("vernon_la")
    s, _ = score.walk_score(f["lat"], f["lng"], f["pois"])
    assert s < 40, s
    # ...and it must be a real, discriminating gap below the dense anchor, not a hair.
    esb = _fx("esb")
    esb_s, _ = score.walk_score(esb["lat"], esb["lng"], esb["pois"])
    assert esb_s - s > 50, (esb_s, s)


def test_empty_pois_is_never_a_zero_score():
    """An empty Overpass response is an ERROR upstream (overpass.OverpassEmpty). It must
    never arrive here as an honest 0 — but if a caller ever passes [], the score is 0 AND
    every category reads count=0, which the UI can see. The guard that matters lives in
    overpass.pois(); this pins the contract."""
    s, breakdown = score.walk_score(40.7484, -73.9857, [])
    assert s == 0
    assert all(v["count"] == 0 and v["nearest_m"] is None for v in breakdown.values())


def test_transit_score_counts_routes_not_stops():
    # ten stops on ONE bus route must not outscore one stop on a rail line
    bus = [{"category": "bus_stop", "lat": 40.7484 + i * 0.0001, "lng": -73.9857,
            "route_refs": ["M4"]} for i in range(10)]
    one_bus, _ = score.transit_score(40.7484, -73.9857, bus, [])
    rail_one, _ = score.transit_score(40.7484, -73.9857, [], [
        {"name": "34 St-Herald Sq", "lat": 40.7497, "lng": -73.9877, "mode": "rail",
         "routes": ["B", "D", "F", "M", "N", "Q", "R", "W"]}])
    assert rail_one > one_bus, (rail_one, one_bus)
    assert 0 <= one_bus <= 100 and 0 <= rail_one <= 100


def test_bundled_rail_is_present_for_every_metro():
    for metro, floor in [("nyc", 400), ("mia", 30), ("la", 90), ("chi", 100)]:
        st = rail.stations(metro)
        assert len(st) >= floor, (metro, len(st))
        assert all({"name", "lat", "lng", "mode"} <= set(s) for s in st)
