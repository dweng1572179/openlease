"""rail.py: bundled static JSON, zero API calls, zero runtime failure modes — plus a
smoke test on the REAL committed bundles (spec Section 7: NYC 496 + Miami 44 + LA 111 +
Chicago 145 =~ 800 stations, <100KB total).

Run: `python -m pytest tests/test_rail.py -v` from openlease/.
"""
import json

from app.models import METRO_KEYS
from app.providers import rail

_RAIL_DIR = rail._DIR


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
        for metro in METRO_KEYS:
            p = _RAIL_DIR / f"{metro}.json"
            assert p.exists(), f"{p} is missing — run `python -m app.data.rail.refresh`"
            data = json.loads(p.read_text())
            assert len(data) > 10, (
                f"{metro}.json has only {len(data)} stations — looks broken, not just stale"
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
