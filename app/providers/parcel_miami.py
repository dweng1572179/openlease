"""Miami-Dade — the county Property Appraiser's ArcGIS FeatureServer, joined by 13-digit
folio.

The trap: the COUNTY zoning layer returns ZERO features for Brickell, Wynwood and
Downtown, because those are incorporated cities that zone themselves. A naive read of that
zero looks like "no zoning" and would silently blank the field for the exact neighborhoods
the app is most used in. So we branch to the municipal layer when the parcel is inside a
known city, and when we have no branch for a municipality we return zoning=None WITH the
reason — never an empty string.

Verified live 2026-07-12 (plan said otherwise on three counts — see docs/implementation-plan.md
Task 9 correction):
  - The PA layer's actual field is `BUILDING_ACTUAL_AREA`, not `BLDG_ACTUAL_AREA`.
  - There is no `MUNICIPALITY`/`MUNIC_NAME` field at all — the municipality the parcel sits
    in is `TRUE_SITE_CITY`.
  - `M21_Zoning` is not a layer on the county's ArcGIS org — the City of Miami's zoning
    (Miami 21) is hosted on the CITY's own GIS server, a MapServer (not FeatureServer) at
    `gis.miami.gov/.../ZoningMiami21/MapServer/5`, and the zone-code field is `M21_ZONE`
    (brief guessed `ZONE`). House-numbered street addresses also lose their ordinal suffix
    in TRUE_SITE_ADDR ("2801 NW 2 AVE", never "2ND") — stripped before the LIKE query."""
import json
import re

import httpx

from ..cache import cached
from ..models import Parcel

PA = ("https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/"
      "PaGISView_gdb/FeatureServer/0/query")
# City of Miami covers Brickell / Wynwood / Downtown / Little Havana / Coconut Grove.
MUNI_ZONING = {
    "MIAMI": ("https://gis.miami.gov/gis/rest/services/Zoning/ZoningMiami21/MapServer/5/query",
              "M21_ZONE"),
}
NO_BRANCH = ("Zoning here is set by the municipality, and OpenLease has no layer wired "
             "for it yet. The county layer covers unincorporated Miami-Dade only.")
_ORDINAL = re.compile(r"(\d+)(ST|ND|RD|TH)\b")  # Miami-Dade's addressing drops ordinals


def normalize(raw: dict, zoning: str | None = None,
              zoning_reason: str | None = None) -> Parcel:
    def num(k, cast=float):
        v = raw.get(k)
        try:
            return cast(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    missing = {}
    if zoning is None:
        missing["zoning"] = zoning_reason or NO_BRANCH
    return Parcel(
        parcel_id=f"mia:{raw['FOLIO']}", metro="mia",
        owner_name=raw.get("TRUE_OWNER1") or None,
        zoning=zoning,
        year_built=num("YEAR_BUILT", int), lot_sqft=num("LOT_SIZE", int),
        bldg_sqft=num("BUILDING_ACTUAL_AREA", int), floors=num("FLOOR_COUNT", int),
        units=num("UNIT_COUNT", int), use_code=raw.get("DOR_DESC") or None,
        missing_reason=missing, raw_json=json.dumps(raw),
    )


def _zoning(muni: str, lat: float, lng: float) -> tuple[str | None, str | None]:
    entry = MUNI_ZONING.get((muni or "").upper())
    if not entry:
        return None, NO_BRANCH
    url, field = entry

    def fetch():
        r = httpx.get(url, params={
            "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": field, "returnGeometry": "false", "f": "json"}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("miami_zoning", muni, {"lat": round(lat, 6), "lng": round(lng, 6)}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None, f"No zoning polygon covers this point in the {muni} layer."
    return feats[0]["attributes"].get(field), None


def lookup(address: str, lat: float | None = None, lng: float | None = None) -> Parcel | None:
    street = _ORDINAL.sub(r"\1", address.split(",")[0].upper())

    def fetch():
        r = httpx.get(PA, params={
            "where": f"TRUE_SITE_ADDR LIKE '{street}%'",
            "outFields": "*", "returnGeometry": "false", "resultRecordCount": 1,
            "f": "json"}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("miami_pa", "address", {"addr": street}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None
    raw = feats[0]["attributes"]
    muni = raw.get("TRUE_SITE_CITY") or ""
    z, reason = (_zoning(muni, lat, lng) if lat and lng else (None, NO_BRANCH))
    return normalize(raw, z, reason)
