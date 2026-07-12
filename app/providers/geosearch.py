"""NYC GeoSearch (geosearch.planninglabs.nyc) — free, keyless, and it hands back the BBL
in `addendum.pad.bbl`, which is exactly the PLUTO join key. The other three metros use
their own ArcGIS/Socrata address search (see each parcel provider)."""
import httpx

from ..cache import cached
from ..config import settings

URL = "https://geosearch.planninglabs.nyc/v2/search"


def geocode(address: str) -> dict | None:
    def fetch():
        r = httpx.get(
            URL, params={"text": address, "size": 1},
            headers={"User-Agent": settings.crawl_user_agent}, timeout=20.0,
        )
        r.raise_for_status()
        return r.json()

    data = cached("geosearch", "search", {"text": address}, fetch)
    feats = data.get("features") or []
    if not feats:
        return None
    f = feats[0]
    lng, lat = f["geometry"]["coordinates"]
    props = f.get("properties", {})
    bbl = (props.get("addendum", {}).get("pad", {}) or {}).get("bbl")
    return {"lat": lat, "lng": lng, "bbl": str(bbl) if bbl else None,
            "borough": props.get("borough"), "matched": props.get("label")}
