"""Cook County (parcel, by 14-digit PIN) + City of Chicago (zoning).

The trap: zoning, floors and FAR come from a CITY OF CHICAGO dataset, so they are NULL for
roughly half of Cook County — every suburb. A suburban parcel with zoning="" would read as
"unzoned", which is nonsense. Return None with the reason instead.

Verified live 2026-07-12 (plan said otherwise on three counts — see docs/implementation-plan.md
Task 9 correction):
  - `3723-97qp` ("Assessor - Parcel Addresses") has no `property_address` column; the real
    column is `prop_address_full`, and it also carries `owner_address_name` — the owner join
    the plan expected to come from the attrs dataset actually lives HERE.
  - `pabr-t5kh` ("Assessor - Parcel Universe") is geographic/tax-district reference data
    (township, census tract, school district...) — it has no year/sqft/stories/owner at all.
    Building characteristics live in `x54s-btds` ("Assessor - Single and Multi-Family
    Improvement Characteristics"), keyed by `char_*` columns.
  - `7cve-jgbp` is a "map" visualization asset (assetType=map) with no queryable SODA rows
    (`$select=*` returns `{}`). The real underlying tabular resource is `dj47-wfun`; the
    `zone_class` field name the plan guessed was otherwise correct.
"""
import json
import re

import httpx

from ..cache import cached
from ..models import Parcel

ADDR = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"   # address -> PIN + owner
ATTRS = "https://datacatalog.cookcountyil.gov/resource/x54s-btds.json"  # PIN -> characteristics
CITY_ZONING = "https://data.cityofchicago.org/resource/dj47-wfun.json"
# PIN -> lat/lon (+ census/tax geography). Neither ADDR nor ATTRS above carries
# coordinates — this is the one Cook County Assessor dataset that does. Verified live
# 2026-07-12.
UNIVERSE = "https://datacatalog.cookcountyil.gov/resource/pabr-t5kh.json"
SUBURB_REASON = ("Zoning is a City of Chicago dataset. This parcel is in suburban Cook "
                 "County, which the city does not zone — the data does not exist, the "
                 "lookup did not fail.")
_STORY_RE = re.compile(r"(\d+)")


def normalize(raw: dict, zoning: str | None = None,
              zoning_reason: str | None = None) -> Parcel:
    def num(k, cast=float):
        # Cook County serializes every numeric column as a decimal STRING ("1972.0",
        # "5742.0", never a clean int) — go through float() first or int("1972.0") raises.
        v = raw.get(k)
        try:
            return cast(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def floors():
        # char_type_resd is a descriptive string ("2 Story", "3 Story +"), not a number —
        # the leading digit IS the story count; this reads it, it does not guess it.
        m = _STORY_RE.match(raw.get("char_type_resd") or "")
        return int(m.group(1)) if m else None

    missing = {}
    if zoning is None:
        missing["zoning"] = zoning_reason or SUBURB_REASON
    return Parcel(
        parcel_id=f"chi:{raw['pin']}", metro="chi",
        owner_name=raw.get("owner_address_name") or raw.get("mail_address_name") or None,
        zoning=zoning,
        year_built=num("char_yrblt", int), lot_sqft=num("char_land_sf", int),
        bldg_sqft=num("char_bldg_sf", int), floors=floors(),
        units=None,   # no clean numeric unit count is published here (char_apts is a word
                      # like "Six") — None because it genuinely isn't parseable, not a fake.
        use_code=raw.get("class") or None,   # Cook's `class` = building class + land use
        missing_reason=missing, raw_json=json.dumps(raw),
    )


def _zoning(lat: float, lng: float) -> tuple[str | None, str | None]:
    def fetch():
        r = httpx.get(CITY_ZONING, params={
            "$where": f"intersects(the_geom, 'POINT ({lng} {lat})')", "$limit": 1},
            timeout=30.0)
        r.raise_for_status()
        return r.json()

    rows = cached("chi_zoning", "point", {"lat": round(lat, 6), "lng": round(lng, 6)}, fetch)
    if not rows:
        return None, SUBURB_REASON
    return rows[0].get("zone_class"), None


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    street = address.split(",")[0].upper()

    def fetch_pin():
        r = httpx.get(ADDR, params={"$where": f"upper(prop_address_full) like '{street}%'",
                                    "$order": "year DESC", "$limit": 1}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    hits = cached("cook_addr", "search", {"addr": street}, fetch_pin)
    if not hits:
        return None
    addr_row = hits[0]
    pin = addr_row.get("pin") or addr_row.get("pin10")

    def fetch_attrs():
        r = httpx.get(ATTRS, params={"pin": pin, "$order": "year DESC", "$limit": 1},
                       timeout=30.0)
        r.raise_for_status()
        return r.json()

    rows = cached("cook_attrs", "pin", {"pin": pin}, fetch_attrs)
    raw = {**addr_row, **(rows[0] if rows else {}), "pin": pin}
    z, reason = (_zoning(lat, lng) if lat and lng else (None, SUBURB_REASON))
    return normalize(raw, z, reason)


def geocode(address: str) -> dict | None:
    """address -> {"lat","lng"}, for crawl-time geocoding (T10) — no new provider, no new
    key. Reuses the SAME address -> PIN step `lookup()` above uses (`ADDR`, cached under
    the identical key, so a prior `lookup()`/`geocode()` call for this address is a cache
    hit, never a second network round trip), then one more free Socrata call to the
    Assessor's own 'Parcel Universe' dataset (`pabr-t5kh`) — the one Cook County dataset
    that actually carries lat/lon per PIN; `ADDR` and `ATTRS` above do not."""
    street = address.split(",")[0].upper()

    def fetch_pin():
        r = httpx.get(ADDR, params={"$where": f"upper(prop_address_full) like '{street}%'",
                                    "$order": "year DESC", "$limit": 1}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    hits = cached("cook_addr", "search", {"addr": street}, fetch_pin)
    if not hits:
        return None
    pin = hits[0].get("pin") or hits[0].get("pin10")
    if not pin:
        return None

    def fetch_geo():
        r = httpx.get(UNIVERSE, params={"pin": pin, "$select": "lat,lon",
                                        "$order": "year DESC", "$limit": 1}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    rows = cached("cook_geo", "pin", {"pin": pin}, fetch_geo)
    if not rows or rows[0].get("lat") is None or rows[0].get("lon") is None:
        return None
    try:
        return {"lat": float(rows[0]["lat"]), "lng": float(rows[0]["lon"])}
    except (TypeError, ValueError):
        return None
