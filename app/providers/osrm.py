"""Airport drive times. ONE keyless OSRM /table call returns every airport in the metro.

OSRM's public router is FREE-FLOW — no traffic. Its Midtown->JFK is 31 minutes against a
real 45-60. That is not a bug to fix, it is a number to LABEL: the UI says "no traffic".
Offline, we fall back to a power law fitted to OSRM's own answers, which underestimates
any route crossing a bridge or water."""
import logging
import math

import httpx

from ..cache import cached
from ..config import settings
from ..models import METROS

log = logging.getLogger(__name__)


def haversine_mi(lat1, lng1, lat2, lng2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def haversine_fallback(lat: float, lng: float, metro: str) -> dict[str, float]:
    """drive_min = 5.31 * miles^0.718 — fitted to OSRM. Underestimates water crossings."""
    out = {}
    for code, (alat, alng) in METROS[metro]["airports"].items():
        mi = haversine_mi(lat, lng, alat, alng)
        out[code] = round(5.31 * (mi ** 0.718), 1)
    return out


def drive_minutes(lat: float, lng: float, metro: str) -> dict[str, float]:
    airports = METROS[metro]["airports"]
    coords = ";".join([f"{lng},{lat}"] + [f"{a[1]},{a[0]}" for a in airports.values()])
    url = f"{settings.osrm_url}/table/v1/driving/{coords}?sources=0&annotations=duration"

    def fetch():
        r = httpx.get(url, headers={"User-Agent": settings.crawl_user_agent}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    try:
        data = cached("osrm", "table", {"lat": round(lat, 5), "lng": round(lng, 5), "metro": metro}, fetch)
        durations = data["durations"][0][1:]          # [0] is the origin to itself
        return {code: round(d / 60.0, 1) for code, d in zip(airports, durations) if d is not None}
    except Exception as e:  # noqa: BLE001 — offline / rate-limited: the power law still answers
        log.warning(
            "OSRM drive_minutes failed (%s: %s) for %s,%s in %s — falling back to the "
            "haversine power law (less accurate, no bridge/water penalty).",
            type(e).__name__, e, lat, lng, metro,
        )
        return haversine_fallback(lat, lng, metro)
