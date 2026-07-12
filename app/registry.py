"""Capability -> active provider. Lazy: a provider only imports when first requested, so
the app boots even before a key is configured. Returns None when a capability has no
usable provider — callers degrade gracefully."""
from functools import lru_cache

from .config import settings


def reset() -> None:
    """Drop every cached provider — called after settings change so the next access
    rebuilds with the current keys."""
    for fn in (geocoder, parcel_provider, poi_provider, embedder):
        fn.cache_clear()


@lru_cache
def geocoder(metro: str):
    if metro == "nyc":
        from .providers import geosearch
        return geosearch
    # the other three geocode through their own parcel provider's address search
    return parcel_provider(metro)


@lru_cache
def parcel_provider(metro: str):
    mod = {"nyc": "parcel_nyc", "mia": "parcel_miami",
           "la": "parcel_la", "chi": "parcel_chicago"}.get(metro)
    if not mod:
        return None
    import importlib
    full_name = f"{__package__}.providers.{mod}"
    try:
        return importlib.import_module(full_name)
    except ModuleNotFoundError as exc:
        # Same guard as settings_store.save(): only swallow the case where THIS module
        # is the one missing (not built until T9). A ModuleNotFoundError for some OTHER
        # import inside an already-built parcel_*.py is a real bug and must not be
        # silently reinterpreted as "not built yet".
        if exc.name != full_name:
            raise
        return None


@lru_cache
def poi_provider():
    from .providers import overpass
    return overpass          # free, no key


@lru_cache
def embedder():
    if not settings.voyage_api_key:
        return None          # BM25-only; RRF over one list is order-preserving
    from .providers.voyage import VoyageEmbedder
    return VoyageEmbedder()
