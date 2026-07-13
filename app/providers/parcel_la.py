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
import re

import httpx

from ..cache import cached
from ..models import Parcel

MAPSERVER = ("https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/"
             "LACounty_Parcel/MapServer/0/query")

# The Assessor stores addresses ABBREVIATED — "1442 2ND ST SANTA MONICA CA 90401", never
# "STREET". So `SitusFullAddress LIKE '1442 2ND STREET%'` matches nothing, and it matched
# nothing for the whole crawled LA corpus: 4 of 74 listings got a pin. Spell it their way.
_ABBREV = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD",
    "PLACE": "PL", "COURT": "CT", "LANE": "LN", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "TERRACE": "TER", "CIRCLE": "CIR", "SQUARE": "SQ", "TRAIL": "TRL", "WAY": "WAY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def _situs(address: str) -> str:
    """The street portion, spelled the way the Assessor spells it."""
    street = address.split(",")[0].upper()
    street = re.sub(r"[^\w\s]", " ", street)                  # "1160-1170" -> "1160 1170"
    return " ".join(_ABBREV.get(w, w) for w in street.split())


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
    where = f"SitusFullAddress LIKE '{_situs(address)}%'" if not lat else "1=1"
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


def _ring_centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """Area-weighted polygon centroid (the shoelace formula) — a plain vertex average
    would skew toward whichever side of an irregular (e.g. L-shaped) parcel happens to
    have more vertices. Good enough for a map pin; not survey-grade. Returns None for a
    degenerate (zero-area) ring rather than dividing by zero."""
    area = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area /= 2
    if not area:
        return None
    return (cy / (6 * area), cx / (6 * area))   # (lat, lng) — outSR=4326 means x=lng, y=lat


def geocode(address: str) -> dict | None:
    """address -> {"lat","lng"}, for crawl-time geocoding (T10) — no new provider, no new
    key. Same free county parcel layer `lookup()` above queries, but with polygon
    geometry requested (`outSR=4326`) so the parcel's own centroid stands in for a
    street-address geocode. LA parcels are POLYGONS (`esriGeometryPolygon`, verified live
    2026-07-12 — unlike Miami's point layer), and this server does not support
    `returnCentroid` (verified live: the parameter is silently ignored, no `centroid` key
    comes back), so the centroid is computed here from the first (exterior) ring."""
    street = _situs(address)

    def fetch():
        r = httpx.get(MAPSERVER, params={
            "where": f"SitusFullAddress LIKE '{street}%'", "outFields": "AIN",
            "returnGeometry": "true", "outSR": 4326, "resultRecordCount": 1,
            "f": "json"}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("la_geocode", "address", {"addr": street}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None
    rings = (feats[0].get("geometry") or {}).get("rings") or []
    if not rings or len(rings[0]) < 3:
        return None
    centroid = _ring_centroid(rings[0])
    return {"lat": centroid[0], "lng": centroid[1]} if centroid else None
