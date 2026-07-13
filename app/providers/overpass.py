"""POIs from OpenStreetMap, at INGEST time only, cached forever.

Two hard-won rules, both spec Section 2:

1. An EMPTY response is an ERROR, never a score of 0. `overpass.osm.ch` is a
   Switzerland-only extract: it returns HTTP 200 with zero elements for US coordinates,
   which silently scores every American listing 0 and looks like working code. So: only
   the allowlisted mirrors, and a zero-element response raises.
2. Query with `nwr`, not `node` — malls, parks and campuses are ways/relations, and a
   node-only query silently misses them. `out center tags` gives every element a point.

One more thing found live (not in the original plan): `overpass-api.de` 406s a request
with httpx's default `python-httpx/x.y.z` User-Agent — it wants a request that identifies
itself. We send `settings.crawl_user_agent` (the same polite identifier the crawler uses)
on every call.
"""
import time
import logging
import httpx

from ..cache import cached
from ..config import settings

RADIUS_M = 2414  # Walk Score's outer bound (1.5 miles)

log = logging.getLogger("openlease")

RETRY_STATUS = (406, 429, 504)   # public-mirror soft rate-limiting, not a bad request
RETRIES = 4
BACKOFF_BASE_S = 8.0

ALLOWED_HOSTS = ("overpass-api.de", "overpass.kumi.systems")

# Walk Score's 9 categories -> OSM tags.
CATEGORIES = {
    "grocery": ('shop', ("supermarket", "grocery", "convenience", "greengrocer")),
    "restaurants": ('amenity', ("restaurant", "fast_food")),
    "shopping": ('shop', ("clothes", "department_store", "mall", "hardware", "electronics")),
    "coffee": ('amenity', ("cafe",)),
    "banks": ('amenity', ("bank",)),
    "parks": ('leisure', ("park", "garden")),
    "schools": ('amenity', ("school",)),
    "books": ('amenity', ("library",)),
    "entertainment": ('amenity', ("cinema", "theatre", "nightclub", "pub", "bar")),
}
_TAG_TO_CATEGORY = {
    (key, val): cat for cat, (key, vals) in CATEGORIES.items() for val in vals
}


class OverpassEmpty(RuntimeError):
    """Zero elements came back. That is a failure, not an empty neighborhood."""


def _query(lat: float, lng: float) -> str:
    parts = []
    for _cat, (key, vals) in CATEGORIES.items():
        parts.append(f'nwr(around:{RADIUS_M},{lat},{lng})[{key}~"^({"|".join(vals)})$"];')
    # bus routes for Transit Score come from the stops' route_ref tag
    parts.append(f'nwr(around:{RADIUS_M},{lat},{lng})[highway=bus_stop];')
    # 120s, not the plan's original 60s: verified live against overpass-api.de that the
    # full 9-category + bus-stop query for a dense downtown point (Empire State Building)
    # takes ~43s of real server-side compute on its own, with no safety margin against a
    # moment of load — and did in fact 504 twice at [timeout:60]. This call is INGEST-TIME
    # ONLY and cached forever, so there is no cost to giving it room; there IS a cost to a
    # false "the mirror is down" failure on the densest, most information-rich addresses.
    return f"[out:json][timeout:120];\n({chr(10).join(parts)}\n);\nout center tags;"


def pois(lat: float, lng: float) -> list[dict]:
    """One call, every category. Cached forever (cost 0 — Overpass is free)."""
    host = httpx.URL(settings.overpass_url).host
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(
            f"{host} is not an allowlisted Overpass mirror {ALLOWED_HOSTS}. "
            "overpass.osm.ch in particular returns 200 + zero elements for US coords."
        )
    q = _query(lat, lng)

    def fetch():
        # 406/429/504 from a public Overpass mirror is soft rate-limiting, not a bad
        # request — under a bulk ingest it hit 9 of 12 listings on the first pass and every
        # one of them succeeded on a retry. Back off and try again rather than throwing the
        # listing away (an ingest that silently drops 75% of its POIs would leave a corpus
        # of plausible-looking, uniformly-low Walk Scores). Accept is explicit: some mirrors
        # 406 a POST that doesn't state what it wants back.
        #
        # And when a mirror has had enough of us, try the OTHER one. That is the entire
        # reason two are allowlisted, and the retry loop was only ever asking the first.
        # The configured mirror is tried first; the rest of the allowlist is the fallback.
        host = httpx.URL(settings.overpass_url).host
        mirrors = [settings.overpass_url] + [
            f"https://{h}/api/interpreter" for h in ALLOWED_HOSTS if h != host]
        last = None
        for mirror in mirrors:
            for attempt in range(RETRIES):
                try:
                    r = httpx.post(
                        mirror, data={"data": q},
                        headers={"User-Agent": settings.crawl_user_agent,
                                 "Accept": "application/json"},
                        timeout=150.0,
                    )
                except httpx.HTTPError as e:              # a timeout is a refusal too
                    log.warning("Overpass %s unreachable (%s) — next mirror",
                                httpx.URL(mirror).host, type(e).__name__)
                    last = e
                    break
                if r.status_code in RETRY_STATUS:
                    wait = BACKOFF_BASE_S * (2 ** attempt)
                    log.warning("Overpass %s %s (attempt %d/%d) — backing off %.0fs",
                                httpx.URL(mirror).host, r.status_code, attempt + 1,
                                RETRIES, wait)
                    last = r
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
        # Every allowlisted mirror refused. Surface it — never a fake 0.
        if hasattr(last, "raise_for_status"):
            last.raise_for_status()
        raise RuntimeError(f"every allowlisted Overpass mirror refused: {last}")

    data = cached("overpass", "interpreter", {"lat": round(lat, 5), "lng": round(lng, 5)}, fetch)
    els = data.get("elements", [])
    if not els:
        raise OverpassEmpty(
            f"Overpass returned zero elements for {lat},{lng}. Treating this as a FAILURE — "
            "a real address always has something within 1.5 miles. Check the mirror."
        )
    return [_normalize(e) for e in els if _normalize(e)]


def _normalize(e: dict) -> dict | None:
    tags = e.get("tags") or {}
    center = e.get("center") or {}
    lat, lng = e.get("lat", center.get("lat")), e.get("lon", center.get("lon"))
    if lat is None or lng is None:
        return None
    if tags.get("highway") == "bus_stop":
        return {"category": "bus_stop", "name": tags.get("name"), "lat": lat, "lng": lng,
                "route_refs": [r.strip() for r in (tags.get("route_ref") or "").split(";") if r.strip()]}
    for (key, val), cat in _TAG_TO_CATEGORY.items():
        if tags.get(key) == val:
            return {"category": cat, "name": tags.get("name"), "lat": lat, "lng": lng,
                    "route_refs": []}
    return None
