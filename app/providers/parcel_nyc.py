"""NYC — PLUTO on Socrata, joined by BBL (from GeoSearch). The one gotcha: `bbl` is a
NUMBER column, so it must be filtered UNQUOTED (`?bbl=1000160100`, not `?bbl='1000…'`) or
Socrata returns nothing at all. PLUTO refreshes ~2x/year, so a parcel is cached forever.

Verified live 2026-07-12: PLUTO's own `bbl` column in the JSON response is NOT the clean
10-digit string GeoSearch hands us — Socrata serializes this NUMBER column with full
decimal precision, e.g. "1008350041.00000000". Used verbatim that turns every parcel_id
into `nyc:1008350041.00000000`; _clean_bbl() below strips it back to the integer BBL."""
import json

import httpx

from ..cache import cached
from ..models import Parcel
from . import geosearch

SOCRATA = "https://data.cityofnewyork.us/resource/64uk-42ks.json"


def _clean_bbl(v) -> str:
    return str(int(float(v)))


def normalize(raw: dict) -> Parcel:
    def num(k, cast=float):
        """Socrata serializes numerics as decimal STRINGS, inconsistently: PLUTO returns
        numfloors as "102.0000000" but yearbuilt as "1931". `int("102.0000000")` raises,
        so a bare int cast silently turned a published floor count into None — which the
        listing page then rendered as "not published in this market" for a field NYC
        publishes for every lot. A silently-dropped field is a WRONG answer, and this is
        the exact failure this whole module exists to prevent. Go through float() first."""
        v = raw.get(k)
        try:
            return cast(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Parcel(
        parcel_id=f"nyc:{_clean_bbl(raw['bbl'])}", metro="nyc",
        owner_name=raw.get("ownername") or None,
        zoning=raw.get("zonedist1") or None,
        far_built=num("builtfar"), far_allowed=num("commfar") or num("residfar"),
        year_built=num("yearbuilt", int), lot_sqft=num("lotarea", int),
        bldg_sqft=num("bldgarea", int), floors=num("numfloors", int),
        units=num("unitstotal", int), use_code=raw.get("landuse") or None,
        raw_json=json.dumps(raw),
    )


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    g = geosearch.geocode(address)
    if not g or not g.get("bbl"):
        return None
    bbl = g["bbl"]

    def fetch():
        r = httpx.get(SOCRATA, params={"bbl": bbl}, timeout=30.0)  # UNQUOTED — it's a NUMBER col
        r.raise_for_status()
        return r.json()

    rows = cached("pluto", "bbl", {"bbl": bbl}, fetch)
    return normalize(rows[0]) if rows else None
