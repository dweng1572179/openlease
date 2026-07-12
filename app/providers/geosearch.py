"""NYC GeoSearch (geosearch.planninglabs.nyc) — free, keyless, and it hands back the BBL
in `addendum.pad.bbl`, which is exactly the PLUTO join key. The other three metros use
their own ArcGIS/Socrata address search (see each parcel provider)."""
import logging
import re

import httpx

from ..cache import cached
from ..config import settings

log = logging.getLogger("openlease")

URL = "https://geosearch.planninglabs.nyc/v2/search"

_WORD = re.compile(r"[a-z0-9]+")
_ORDINAL = re.compile(r"^(\d+)(st|nd|rd|th)$")


def _norm(tok: str) -> str:
    """"5th" -> "5". We ask for "350 5th Ave"; GeoSearch answers "350 5 AVENUE"."""
    m = _ORDINAL.match(tok)
    return m.group(1) if m else tok


def _street_token(address: str) -> str | None:
    """The street NAME in "205 Hallock Road, Stony Brook NY" -> "hallock". The first word
    after the house number, which is the one part of an address a geocoder cannot fudge."""
    toks = _WORD.findall(address.lower())
    if len(toks) < 2 or not toks[0].isdigit():
        return None
    return next((_norm(t) for t in toks[1:] if not t.isdigit()), None)


def geocode(address: str) -> dict | None:
    """None when we cannot place the address. NEVER a confident wrong answer.

    GeoSearch only covers the five boroughs and it does NOT decline: asked for
    "205 Hallock Road, Stony Brook NY" it returns "205 DAHILL ROAD, Brooklyn" — a different
    street, in a different place — and reports `match_type: fallback` with confidence 0.8.
    It reports exactly the same match_type for a correct hit, so its own confidence signal
    cannot separate them. A national feed crawled under `nyc` therefore had every Long
    Island and out-of-state address silently pinned somewhere in Brooklyn and handed a New
    York Walk Score.

    So we check the one thing a geocoder cannot fudge: the street name we asked for has to
    appear in the address it gives back.
    """
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
    props = f.get("properties", {})
    label = props.get("label") or ""

    want = _street_token(address)
    if want and want not in [_norm(t) for t in _WORD.findall(label.lower())]:
        # It found *a* building, just not the one we asked for.
        log.info("geosearch: asked for %r, got %r — rejecting (wrong street)", address, label)
        return None

    lng, lat = f["geometry"]["coordinates"]
    bbl = (props.get("addendum", {}).get("pad", {}) or {}).get("bbl")
    return {"lat": lat, "lng": lng, "bbl": str(bbl) if bbl else None,
            "borough": props.get("borough"), "matched": label}
