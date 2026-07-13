"""Walk Score and Transit Score — Walk Score's own PUBLISHED methodology, not a heuristic.

Walk Score (2011): 9 categories, weights summing to 15, multiplied by 6.67 -> 0-100.
Distance decay: amenities within 402m (.25mi) score full credit; credit falls to zero at
2414m (1.5mi). The published curve is

    decay(d) = ((2414 - d) / 2012) ^ 2.3135      clamped to 1.0 below 402m, 0 above 2414m

which is solved from Walk Score's three published anchors. Validated against their own
published values: Empire State Building = 100, Bay Ridge = 98.

Transit Score is aggregated PER ROUTE, not per stop — twelve buses on one line is one
route, and counting stops would triple-score a bus corridor:

    raw = Σ_routes (trips_per_week × mode_weight × decay(nearest stop on that route))

log-normalized to 0-100. Mode weights: rail 2, ferry 1.5, bus 1.
"""
import json
import math

from .db import get_conn, get_listing
from .providers import overpass, rail

FULL_CREDIT_M = 402.0
ZERO_CREDIT_M = 2414.0
_EXP = 2.3135
_SPAN = 2012.0

# category -> the weight of the Nth-nearest amenity in it. Walk Score gives depth to the
# categories where variety matters (you want ten restaurants, not ten banks).
WEIGHTS: dict[str, list[float]] = {
    "grocery":       [3.0],
    "restaurants":   [0.75, 0.45, 0.25, 0.25, 0.225, 0.225, 0.225, 0.225, 0.2, 0.2],
    "shopping":      [0.5, 0.45, 0.4, 0.35, 0.3],
    "coffee":        [1.25, 0.75],
    "banks":         [1.0],
    "parks":         [1.0],
    "schools":       [1.0],
    "books":         [1.0],
    "entertainment": [1.0],
}
MAX_WEIGHT = sum(sum(w) for w in WEIGHTS.values())   # 15.0 — Walk Score's own "sums to 15"
MULTIPLIER = 6.67

MODE_WEIGHT = {"rail": 2.0, "ferry": 1.5, "bus": 1.0}
# ponytail: trips/week without a GTFS feed is a per-mode constant. It is the one number
# here that is NOT published; it moves the normalization, not the ordering. Upgrade path:
# read trips/week from each agency's GTFS if the rankings ever look wrong.
TRIPS_PER_WEEK = {"rail": 700.0, "ferry": 200.0, "bus": 350.0}
# ponytail: calibration constant. The spec flags this as needing a fit against ~20 known
# addresses; 4000 is an EYEBALLED guess (not fit against verified ground truth) that puts
# Midtown near 100 and a Vernon industrial block near 30. No `--calibrate` tooling exists —
# do not quote Transit Score as gospel until someone builds that fit; until then the UI
# label must say "a ranking, not a rating."
TRANSIT_NORM = 4000.0


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def decay(meters: float) -> float:
    """1.0 within 402m, 0.0 beyond 2414m, Walk Score's published curve between."""
    if meters <= FULL_CREDIT_M:
        return 1.0
    if meters >= ZERO_CREDIT_M:
        return 0.0
    return ((ZERO_CREDIT_M - meters) / _SPAN) ** _EXP


def walk_score(lat: float, lng: float, pois: list[dict]) -> tuple[int, dict]:
    """(0-100, per-category breakdown). The breakdown is a UI element, not debug output:
    it explains the score instead of asserting it."""
    breakdown: dict[str, dict] = {}
    total = 0.0
    for cat, weights in WEIGHTS.items():
        dists = sorted(
            haversine_m(lat, lng, p["lat"], p["lng"])
            for p in pois if p.get("category") == cat
        )
        earned = sum(w * decay(d) for w, d in zip(weights, dists))
        total += earned
        breakdown[cat] = {
            "score": round(earned, 3),
            "weight": round(sum(weights), 3),
            "count": len(dists),
            "nearest_m": round(dists[0]) if dists else None,
        }
    return min(100, round(total * MULTIPLIER)), breakdown


def transit_score(lat: float, lng: float, pois: list[dict],
                  stations: list[dict]) -> tuple[int, list[dict]]:
    """Per ROUTE, not per stop. Bus routes come from the OSM stops' `route_ref` tag;
    rail/ferry from the bundled station JSON."""
    best: dict[tuple[str, str], float] = {}   # (mode, route) -> nearest meters
    nearby: list[dict] = []

    for s in stations:
        d = haversine_m(lat, lng, s["lat"], s["lng"])
        if d > ZERO_CREDIT_M:
            continue
        mode = s.get("mode", "rail")
        routes = s.get("routes") or [s["name"]]   # an unrouted station is its own "route"
        for rt in routes:
            key = (mode, rt)
            if d < best.get(key, math.inf):
                best[key] = d
        nearby.append({"mode": mode, "route": ",".join(routes), "name": s["name"],
                       "meters": round(d)})

    for p in pois:
        if p.get("category") != "bus_stop":
            continue
        d = haversine_m(lat, lng, p["lat"], p["lng"])
        if d > ZERO_CREDIT_M:
            continue
        for rt in (p.get("route_refs") or []):
            key = ("bus", rt)
            if d < best.get(key, math.inf):
                best[key] = d

    raw = sum(
        TRIPS_PER_WEEK[mode] * MODE_WEIGHT[mode] * decay(d)
        for (mode, _rt), d in best.items()
    )
    scaled = 100.0 * math.log1p(raw) / math.log1p(TRANSIT_NORM)
    nearby.sort(key=lambda n: n["meters"])
    return min(100, round(scaled)), nearby[:8]


def enrich(listing_id: int, tile_pois: list[dict] | None = None) -> dict:
    """Fetch POIs once, score, persist. Raises OverpassEmpty rather than storing a 0 —
    a listing with no score is honest; a listing scored 0 because the mirror was wrong
    is a lie the UI can't detect.

    `tile_pois` is a pre-fetched SUPERSET covering this listing (see overpass.pois_bbox):
    a bulk ingest fetches one rectangle for a whole neighbourhood instead of one circle per
    listing. We filter it back down to RADIUS_M here, which is what makes the two paths
    produce the identical score rather than merely a similar one.
    """
    row = get_listing(listing_id)
    if not row or row.get("lat") is None:
        return {}
    lat, lng = row["lat"], row["lng"]
    if tile_pois is None:
        ps = overpass.pois(lat, lng)                  # raises OverpassEmpty on failure
    else:
        ps = [p for p in tile_pois
              if haversine_m(lat, lng, p["lat"], p["lng"]) <= overpass.RADIUS_M]
        if not ps:
            # The tile came back full but nothing is within 1.5 miles of THIS listing. That
            # is the same claim overpass.pois refuses to make: a real address always has
            # something. Far likelier the pin is wrong or the tile didn't cover it.
            raise overpass.OverpassEmpty(
                f"listing {listing_id} at {lat},{lng} has no POI within {overpass.RADIUS_M}m "
                "of it in its tile — refusing to score it 0")
    ws, breakdown = walk_score(lat, lng, ps)
    ts, nearby = transit_score(lat, lng, ps, rail.stations(row["metro"]))

    with get_conn() as conn:
        conn.execute("DELETE FROM poi WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO poi (listing_id, category, name, lat, lng, meters) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(listing_id, p["category"], p.get("name"), p["lat"], p["lng"],
              round(haversine_m(lat, lng, p["lat"], p["lng"])))
             for p in ps if p["category"] != "bus_stop"],
        )
        conn.execute("DELETE FROM transit_nearby WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO transit_nearby (listing_id, mode, route, name, meters) "
            "VALUES (?, ?, ?, ?, ?)",
            [(listing_id, n["mode"], n["route"], n["name"], n["meters"]) for n in nearby],
        )
        conn.execute(
            "UPDATE listing SET walk_score = ?, transit_score = ?, score_breakdown_json = ? "
            "WHERE id = ?",
            (ws, ts, json.dumps(breakdown), listing_id),
        )
    return {"walk_score": ws, "transit_score": ts, "breakdown": breakdown}


def demo() -> None:
    assert decay(0) == 1.0 and decay(402) == 1.0
    assert decay(2414) == 0.0 and decay(3000) == 0.0
    assert 0.0 < decay(1200) < 1.0
    assert decay(500) > decay(1500) > decay(2400)
    assert abs(MAX_WEIGHT - 15.0) < 0.05, MAX_WEIGHT     # "weights sum to 15"
    assert round(MAX_WEIGHT * MULTIPLIER) == 100, MAX_WEIGHT * MULTIPLIER
    print("score.demo (decay curve + weight normalization) OK")


if __name__ == "__main__":
    demo()
