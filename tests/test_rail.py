"""rail.py: bundled static JSON, zero API calls, zero runtime failure modes — plus a
smoke test on the REAL committed bundles (spec Section 7: NYC 496 + Miami 44 + LA 111 +
Chicago 145 =~ 800 stations, <100KB total).

Run: `python -m pytest tests/test_rail.py -v` from openlease/.
"""
import json

from app.models import METRO_KEYS
from app.providers import rail

_RAIL_DIR = rail._DIR

# Real counts as generated from each agency's open data (spec §7). Both the NYC dedup bug
# (496 -> 379) and the Chicago schema drift (145 -> 0) produced plausible-looking files.
_EXPECTED_STATIONS = {"nyc": 496, "mia": 44, "la": 111, "chi": 145}


def _clear():
    rail.stations.cache_clear()


def test_stations_reads_the_bundled_json_for_a_metro(monkeypatch, tmp_path):
    monkeypatch.setattr(rail, "_DIR", tmp_path)
    (tmp_path / "zz.json").write_text(json.dumps(
        [{"name": "Test Station", "lat": 1.0, "lng": 2.0, "mode": "rail", "routes": ["A"]}]
    ))
    _clear()
    try:
        result = rail.stations("zz")
    finally:
        _clear()  # don't leak a cached tmp_path-backed result into later tests

    assert result == [{"name": "Test Station", "lat": 1.0, "lng": 2.0,
                        "mode": "rail", "routes": ["A"]}]


def test_stations_returns_empty_list_for_a_metro_with_no_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(rail, "_DIR", tmp_path)
    _clear()
    try:
        assert rail.stations("nonexistent") == []
    finally:
        _clear()


def test_stations_is_cached_across_repeated_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(rail, "_DIR", tmp_path)
    (tmp_path / "zz.json").write_text(json.dumps([{"name": "A", "lat": 1, "lng": 2,
                                                     "mode": "rail", "routes": []}]))
    _clear()
    try:
        first = rail.stations("zz")
        (tmp_path / "zz.json").write_text(json.dumps([]))  # file changes underneath...
        second = rail.stations("zz")                        # ...but lru_cache still hits
        assert second == first == [{"name": "A", "lat": 1, "lng": 2,
                                     "mode": "rail", "routes": []}]
    finally:
        _clear()


def test_real_bundled_rail_files_exist_and_are_well_formed():
    """The actual generated bundles (`python -m app.data.rail.refresh`), committed to the
    repo. Order-of-magnitude checks only — exact counts drift slightly whenever an agency
    opens/closes a station, but a metro with a handful of rows or a >100KB total means the
    generator (or an upstream schema) broke, same as it did for Chicago before the fix."""
    _clear()
    total_bytes = 0
    total_stations = 0
    try:
        for metro, expected in _EXPECTED_STATIONS.items():
            p = _RAIL_DIR / f"{metro}.json"
            assert p.exists(), f"{p} is missing — run `python -m app.data.rail.refresh`"
            data = json.loads(p.read_text())
            # A ±5% band, not an order-of-magnitude one. An agency opening or closing a
            # station moves this by 1; the bugs this file exists to catch move it by tens.
            # The NYC generator originally deduped on `name`, silently collapsing 496 real
            # stations to 379 (-24%) — a `> 10` bound sails straight past that, which is
            # exactly the "wrong data that looks like it worked" failure this project keeps
            # hitting. Widen the band if an agency really does open a line; do not remove it.
            lo, hi = round(expected * 0.95), round(expected * 1.05)
            assert lo <= len(data) <= hi, (
                f"{metro}.json has {len(data)} stations, expected ~{expected} (band {lo}-{hi}). "
                f"A big drop usually means the dedup key or an upstream field name broke."
            )
            for row in data:
                assert row.keys() >= {"name", "lat", "lng", "mode", "routes"}
                assert row["mode"] in ("rail", "ferry")
                assert -90 <= row["lat"] <= 90
                assert -180 <= row["lng"] <= 180
            total_stations += len(data)
            total_bytes += p.stat().st_size
    finally:
        _clear()

    assert total_bytes < 100_000, f"rail bundle is {total_bytes} bytes, spec caps it at 100KB"
    assert total_stations > 500, f"only {total_stations} stations total, expected ~800"
