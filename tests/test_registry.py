"""registry.py: capability -> active provider, lazy-loaded and lru_cache'd. This is also
the module whose mere existence flips settings_store.save()'s guarded import live — see
tests/test_settings_store.py for that half of the contract.

Run: `python -m pytest tests/test_registry.py -v` from openlease/.
"""
import pytest

from app import registry
from app.config import settings
from app.providers import geosearch, overpass, parcel_chicago, parcel_la, parcel_miami, parcel_nyc


@pytest.fixture(autouse=True)
def _reset_registry():
    """lru_cache on a module-level function persists across tests otherwise — reset before
    AND after so this file's assertions aren't order-dependent on other test modules."""
    registry.reset()
    yield
    registry.reset()


def test_poi_provider_is_the_overpass_module():
    assert registry.poi_provider() is overpass


def test_geocoder_nyc_is_the_geosearch_module():
    assert registry.geocoder("nyc") is geosearch


@pytest.mark.parametrize("metro", ["mia", "la", "chi"])
def test_geocoder_for_non_nyc_metros_delegates_to_parcel_provider(metro):
    """Now that Task 9 has landed a real parcel_* module for every metro, geocoder(metro)
    delegates to it (its own address search doubles as the geocode) — not a crash, not a
    fake geocoder, and definitely not None any more."""
    assert registry.geocoder(metro) is registry.parcel_provider(metro)
    assert registry.geocoder(metro) is not None


@pytest.mark.parametrize("metro,mod", [
    ("nyc", parcel_nyc), ("mia", parcel_miami), ("la", parcel_la), ("chi", parcel_chicago),
])
def test_parcel_provider_is_the_matching_module_for_every_metro(metro, mod):
    assert registry.parcel_provider(metro) is mod


def test_parcel_provider_is_none_for_an_unknown_metro_key():
    assert registry.parcel_provider("not-a-real-metro") is None


def test_embedder_is_none_without_a_voyage_key(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", "")
    registry.reset()
    assert registry.embedder() is None


def test_reset_clears_every_provider_cache():
    # populate every lru_cache
    registry.poi_provider()
    registry.geocoder("nyc")
    registry.parcel_provider("mia")
    registry.embedder()
    assert registry.poi_provider.cache_info().currsize == 1
    assert registry.geocoder.cache_info().currsize == 1
    assert registry.parcel_provider.cache_info().currsize == 1
    assert registry.embedder.cache_info().currsize == 1

    registry.reset()

    assert registry.poi_provider.cache_info().currsize == 0
    assert registry.geocoder.cache_info().currsize == 0
    assert registry.parcel_provider.cache_info().currsize == 0
    assert registry.embedder.cache_info().currsize == 0


def test_parcel_provider_does_not_swallow_a_real_bug_inside_a_built_module(monkeypatch):
    """The guard that returns None for 'not built yet' must only fire for THIS module
    missing — not for a genuine ModuleNotFoundError raised from inside an already-built
    parcel_*.py (e.g. a typo'd import). Mirrors the settings_store.save() guard."""
    def _fake_import(name, *a, **kw):
        assert name == "app.providers.parcel_nyc"
        raise ModuleNotFoundError("No module named 'totally_unrelated_dependency'",
                                   name="totally_unrelated_dependency")

    monkeypatch.setattr("importlib.import_module", _fake_import)

    with pytest.raises(ModuleNotFoundError, match="totally_unrelated_dependency"):
        registry.parcel_provider("nyc")
