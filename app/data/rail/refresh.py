"""Build-time only: regenerate the bundled rail-station JSON from each agency's open data.
Not imported by the app — the app reads the JSON. Run when an agency opens a station:

    python -m app.data.rail.refresh

Sources (all keyless, verified 2026-07-11, spec Section 7):
  nyc — data.ny.gov 39hk-dx4f (subway entrances/stations, 496)
  mia — Miami-Dade ArcGIS MetroRailStations_gdb (23) + MetroMoverStations_gdb (21)
  la  — LA Metro GTFS gitlab.com/LACMTA/gtfs_rail -> stops.txt where location_type=1 (111)
  chi — data.cityofchicago.org 3tzw-cg4m (CTA 'L' stops, 145)

Two corrections made after hitting the real endpoints (docs/implementation-plan.md Task 7
originally had both wrong — see the commit message / task-7-report.md):

  1. NYC: the dataset's `stop_name` is NOT unique — 76 station names (e.g. "Canal St",
     "Times Sq-42 St") repeat across separate physical stops serving different lines/
     divisions. Deduping on name silently collapsed 496 rows to 379. `gtfs_stop_id` is
     the actual 1:1 key (496 unique of 496 rows) — dedup on that instead.
  2. Chicago: 3tzw-cg4m's schema has changed since the plan was written. There is no
     `station_name` column and no per-line boolean columns (`red`, `blue`, `g`, ...) --
     matching on those silently produced ZERO rows every time (`station_name` is always
     None -> every row skipped). The real columns are `longname` (station name),
     `the_geom` (GeoJSON Point, `[lng, lat]`), and a free-text `lines` field like
     "Brown, Orange, Pink, Purple (Express), Green" — routes are recovered by searching
     that string for the 8 CTA line-color names.
"""
import csv
import io
import json
import re
import zipfile
from pathlib import Path

import httpx

OUT = Path(__file__).parent

_CTA_COLORS = ("Red", "Blue", "Brown", "Green", "Orange", "Pink", "Purple", "Yellow")


def _write(metro: str, rows: list[dict]) -> None:
    (OUT / f"{metro}.json").write_text(json.dumps(rows, indent=0))
    print(f"{metro}: {len(rows)} stations")


def nyc() -> None:
    r = httpx.get("https://data.ny.gov/resource/39hk-dx4f.json",
                  params={"$limit": 2000}, timeout=60.0)
    r.raise_for_status()
    seen, rows = set(), []
    for s in r.json():
        # `gtfs_stop_id` is the real 1:1 key (see module docstring) — `stop_name` repeats
        # across distinct physical stops and must NOT be used to dedup.
        stop_id = s.get("gtfs_stop_id")
        name = s.get("stop_name") or s.get("station_name")
        lat = s.get("gtfs_latitude") or s.get("latitude")
        lng = s.get("gtfs_longitude") or s.get("longitude")
        if not (stop_id and name and lat and lng) or stop_id in seen:
            continue
        seen.add(stop_id)
        rows.append({"name": name, "lat": float(lat), "lng": float(lng), "mode": "rail",
                     "routes": (s.get("daytime_routes") or "").split()})
    _write("nyc", rows)


def mia() -> None:
    rows = []
    for url in [
        "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/MetroRailStations_gdb/FeatureServer/0/query",
        "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/MetroMoverStations_gdb/FeatureServer/0/query",
    ]:
        r = httpx.get(url, params={"where": "1=1", "outFields": "*", "f": "geojson"}, timeout=60.0)
        r.raise_for_status()
        for f in r.json().get("features", []):
            lng, lat = f["geometry"]["coordinates"]
            p = f["properties"]
            rows.append({"name": p.get("NAME") or p.get("STATION"), "lat": lat, "lng": lng,
                         "mode": "rail", "routes": []})
    _write("mia", rows)


def la() -> None:
    r = httpx.get("https://gitlab.com/LACMTA/gtfs_rail/-/raw/master/gtfs_rail.zip",
                  follow_redirects=True, timeout=120.0)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        stops = list(csv.DictReader(io.TextIOWrapper(z.open("stops.txt"), "utf-8-sig")))
    rows = [{"name": s["stop_name"], "lat": float(s["stop_lat"]), "lng": float(s["stop_lon"]),
             "mode": "rail", "routes": []}
            for s in stops if s.get("location_type") == "1"]
    _write("la", rows)


def chi() -> None:
    r = httpx.get("https://data.cityofchicago.org/resource/3tzw-cg4m.json",
                  params={"$limit": 1000}, timeout=60.0)
    r.raise_for_status()
    seen, rows = set(), []
    for s in r.json():
        # real schema: `longname` + `the_geom` + free-text `lines` (see module docstring;
        # the plan's original `station_name`/`location`/per-color-boolean columns don't exist).
        sid = s.get("station_id")
        name = s.get("longname")
        coords = (s.get("the_geom") or {}).get("coordinates") or [None, None]
        lng, lat = coords[0], coords[1]
        if not (sid and name and lat and lng) or sid in seen:
            continue
        seen.add(sid)
        lines = s.get("lines") or ""
        routes = [c for c in _CTA_COLORS if re.search(rf"\b{c}\b", lines)]
        rows.append({"name": name, "lat": float(lat), "lng": float(lng), "mode": "rail",
                     "routes": routes})
    _write("chi", rows)


if __name__ == "__main__":
    for fn in (nyc, mia, la, chi):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — one agency being down must not block the rest
            print(f"{fn.__name__} FAILED: {type(e).__name__}: {e}")
