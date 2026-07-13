"""US Census Bureau geocoder — free, keyless, national, and government-run (zero ToS
surface, like the rest of our public-data layer).

This exists because the per-metro parcel layers are PARCEL CACHES, not geocoders, and they
fail in two different ways that both end in bad data:

  * They miss. LA County's `LACounty_Parcel` resolved 2 of 6 real Los Angeles addresses —
    it did not have "540 Rose Avenue, Venice" at all. Four of 74 crawled LA listings got a
    map pin.
  * They fuzz. A metro-scoped geocoder does not decline: NYC GeoSearch, asked for a street
    in Stony Brook, hands back a *different street in Brooklyn* with the same confidence it
    reports for a correct hit.

So: try the metro's own provider first (it is authoritative, and for NYC it also returns the
BBL we need for PLUTO), and fall back to Census when it comes up empty. Census returns the
address it actually matched, so we can hold it to the same standard as everything else —
the street we asked for has to be the street we got back. A miss is None, never a guess.
"""
import logging
import re

import httpx

from . import addrmatch
from ..cache import cached
from ..config import settings

log = logging.getLogger("openlease")

URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

_WORD = re.compile(r"[a-z0-9]+")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")
# The Census returns "540 ROSE AVE"; a broker writes "540 Rose Avenue". Same street.
_ABBREV = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr", "road": "rd",
    "place": "pl", "court": "ct", "lane": "ln", "parkway": "pkwy", "highway": "hwy",
    "terrace": "ter", "circle": "cir", "square": "sq", "trail": "trl",
    "north": "n", "south": "s", "east": "e", "west": "w",
}


def _norm(tok: str) -> str:
    m = _ORDINAL.match(tok)
    if m:
        return m.group(1)
    return _ABBREV.get(tok, tok)


def _street_token(address: str) -> str | None:
    """The street NAME — the first word after the house number. It is the one part of an
    address a geocoder cannot fudge without being obviously wrong."""
    toks = _WORD.findall(address.lower())
    if len(toks) < 2 or not toks[0].isdigit():
        return None
    return next((_norm(t) for t in toks[1:] if not t.isdigit()), None)


def geocode(address: str) -> dict | None:
    """A full one-line address ("540 Rose Avenue, Venice, CA") -> {"lat","lng"}, or None.

    Free and keyless, but still cached: the Census is a public service and we are not
    entitled to re-ask it the same question.
    """
    def fetch():
        r = httpx.get(URL, params={"address": address, "benchmark": "Public_AR_Current",
                                   "format": "json"},
                      headers={"User-Agent": settings.crawl_user_agent}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    data = cached("census", "onelineaddress", {"address": address}, fetch)
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None

    m = matches[0]
    matched = m.get("matchedAddress") or ""
    if not addrmatch.matches(address, matched):
        log.info("census: asked for %r, got %r — rejecting (not the address we asked for)",
                 address, matched)
        return None

    c = m["coordinates"]
    return {"lat": c["y"], "lng": c["x"], "matched": matched}
