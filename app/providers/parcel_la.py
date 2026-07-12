"""LA County Assessor, joined by 10-digit AIN.

There is NO owner name, and there never will be: California statute does not make
owner-of-record free and public through the county's open GIS. An LA listing therefore
shows fewer fields BY DESIGN. That is a documented `missing_reason`, not a bug and not a
scraping opportunity — if the UI ever renders a blank there instead of the reason, the app
is lying about what it knows.

Verified live 2026-07-12 (plan said otherwise — see docs/implementation-plan.md Task 9
correction): there is no flat `YearBuilt`/`SQFTmain`/`Units` column. A parcel can carry up
to 5 separate structures ("designs"), so the Assessor numbers every building field
`YearBuilt1..5` / `SQFTmain1..5` / `Units1..5`; we read design 1 (the primary structure).
There is also no standalone lot-size attribute — `outFields=*` already returns the
ArcGIS-computed `Shape.STArea()` (the parcel polygon's own area, in the service's native
square feet), which is the only honest source for `lot_sqft` here."""
import json

import httpx

from ..cache import cached
from ..models import Parcel

MAPSERVER = ("https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/"
             "LACounty_Parcel/MapServer/0/query")
OWNER_REASON = ("California statute: owner-of-record is not published free through the "
                "county's open GIS. This is a gap in the public data, not a failed lookup.")
ZONING_REASON = "LA zoning lives in a separate county layer; not wired in v1."


def normalize(raw: dict) -> Parcel:
    def num(k, cast=float):
        v = raw.get(k)
        try:
            return cast(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Parcel(
        parcel_id=f"la:{raw['AIN']}", metro="la",
        owner_name=None,                      # never available — see OWNER_REASON
        zoning=None,
        year_built=num("YearBuilt1", int), lot_sqft=num("Shape.STArea()", int),
        bldg_sqft=num("SQFTmain1", int), units=num("Units1", int),
        use_code=raw.get("UseType") or raw.get("UseDescription") or None,
        missing_reason={"owner_name": OWNER_REASON, "zoning": ZONING_REASON},
        raw_json=json.dumps(raw),
    )


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    where = (f"SitusFullAddress LIKE '{address.split(',')[0].upper()}%'" if not lat
             else "1=1")
    params = {"where": where, "outFields": "*", "returnGeometry": "false",
              "resultRecordCount": 1, "f": "json"}
    if lat and lng:
        params |= {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
                   "inSR": 4326, "spatialRel": "esriSpatialRelIntersects"}

    def fetch():
        r = httpx.get(MAPSERVER, params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("la_parcel", "query", {"addr": address, "lat": lat, "lng": lng}, fetch)
    feats = data.get("features") or []
    return normalize(feats[0]["attributes"]) if feats else None
