"""Provider interfaces. Each capability is one small Protocol; a concrete provider
implements it and is registered in registry.py. Every provider wraps its network calls
in cache.cached()."""
from typing import Protocol, runtime_checkable

from ..models import Parcel


@runtime_checkable
class Geocoder(Protocol):
    def geocode(self, address: str) -> dict | None:
        """address -> {lat, lng, matched, ...metro-specific join key} or None."""
        ...


@runtime_checkable
class ParcelProvider(Protocol):
    def lookup(self, address: str, lat: float | None, lng: float | None) -> Parcel | None:
        """Address (or point) -> the metro's parcel record, normalized. A field this
        metro does not publish is None WITH a missing_reason — never 0, never a fake."""
        ...


@runtime_checkable
class PoiProvider(Protocol):
    def pois(self, lat: float, lng: float) -> list[dict]:
        """[{category, name, lat, lng, route_refs}] within the Walk Score radius."""
        ...


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """L2-normalized vectors, one per text."""
        ...
