"""osrm.py: one keyless OSRM /table call for every metro airport, free-flow (no traffic),
with an offline power-law fallback when the network call fails.

Run: `python -m pytest tests/test_osrm.py -v` from openlease/.
"""
import logging

import httpx as real_httpx
import pytest

from app import db
from app.config import settings
from app.models import METROS
from app.providers import osrm


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "osrm.db"))
    db.init_db()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_drive_minutes_converts_seconds_to_rounded_minutes(isolated_db, monkeypatch):
    # nyc airports, in metros.yml order: JFK, LGA, EWR
    seen = {}

    def _fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        # origin-to-self is index 0; JFK/LGA/EWR follow in request order
        return _FakeResponse({"durations": [[0.0, 1860.0, 900.5, 2401.9]]})

    monkeypatch.setattr("httpx.get", _fake_get)

    out = osrm.drive_minutes(40.7580, -73.9855, "nyc")

    assert out == {"JFK": 31.0, "LGA": 15.0, "EWR": 40.0}
    assert "/table/v1/driving/" in seen["url"]
    assert seen["headers"]["User-Agent"] == settings.crawl_user_agent


def test_drive_minutes_falls_back_to_haversine_on_network_failure(isolated_db, monkeypatch, caplog):
    def _raise(*a, **kw):
        raise real_httpx.ConnectError("simulated offline")

    monkeypatch.setattr("httpx.get", _raise)

    with caplog.at_level(logging.WARNING, logger="app.providers.osrm"):
        out = osrm.drive_minutes(40.7580, -73.9855, "nyc")

    assert out == osrm.haversine_fallback(40.7580, -73.9855, "nyc")
    assert set(out) == set(METROS["nyc"]["airports"])
    assert any("falling back" in r.message for r in caplog.records), (
        "the OSRM->haversine fallback must be logged loudly, not silently swallowed"
    )


def test_drive_minutes_falls_back_when_response_shape_is_unexpected(isolated_db, monkeypatch):
    """A malformed/partial response (e.g. a mid-request timeout mid-JSON) must degrade to
    the offline fallback rather than raising out of drive_minutes()."""
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeResponse({"unexpected": True}))

    out = osrm.drive_minutes(40.7580, -73.9855, "nyc")

    assert out == osrm.haversine_fallback(40.7580, -73.9855, "nyc")


def test_haversine_fallback_matches_the_fitted_power_law():
    lat, lng = 40.7580, -73.9855
    out = osrm.haversine_fallback(lat, lng, "nyc")
    for code, (alat, alng) in METROS["nyc"]["airports"].items():
        mi = osrm.haversine_mi(lat, lng, alat, alng)
        assert out[code] == round(5.31 * (mi ** 0.718), 1)


def test_haversine_of_a_point_with_itself_is_zero():
    assert osrm.haversine_mi(40.75, -73.98, 40.75, -73.98) == 0.0
