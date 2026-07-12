"""Bring-your-own supply: the city's vacancy feed, and your own CSV (a broker export, a
CoStar pull — whatever you already licensed). Neither touches a broker site."""
import csv
import io

from fastapi import Depends, UploadFile

from . import db, score
from .app import app, require_auth
from .models import METRO_KEYS
from .providers import gov_nyc

# CSV column -> Listing field. Anything else in the file is ignored.
CSV_MAP = {
    "address": "address", "neighborhood": "neighborhood", "borough": "borough",
    "type": "property_type", "property_type": "property_type",
    "size": "size_sf", "size_sf": "size_sf", "sf": "size_sf",
    "rent": "asking_rent", "asking_rent": "asking_rent",
    "rent_unit": "rent_unit", "lease_type": "lease_type",
    "price": "sale_price", "sale_price": "sale_price",
    "lat": "lat", "lng": "lng", "broker": "broker_name", "broker_firm": "broker_firm",
    "phone": "broker_phone", "email": "broker_email", "url": "source_url",
    "description": "our_description",   # YOUR file, YOUR words — you own this one
}
_NUM = {"size_sf", "sale_price"}
_FLOAT = {"asking_rent", "lat", "lng"}


@app.post("/api/import/storefronts")
def import_storefronts(limit: int = 500, _=Depends(require_auth)):
    """NYC only — it is the only one of the four metros that publishes a vacancy feed.

    Import stores the leads and stops there. Scoring is `POST /api/enrich`, the same
    separate paced pass the crawler uses (`crawl.enrich_pending` already selects exactly
    these rows: lat IS NOT NULL AND walk_score IS NULL).

    This used to call score.enrich() inline, unpaced, inside the loop, and swallow every
    failure with a bare `except: pass`. At the default limit that fired up to 500
    back-to-back Overpass calls — each with retries and 8-64s of backoff — and discarded
    every failure without a single log line. Same coupling that throttled the crawler, plus
    the only completely silent provider failure left in the codebase.
    """
    recs = gov_nyc.storefronts(limit=limit)
    saved = sum(bool(db.save_listing(rec)) for rec in recs)
    return {"fetched": len(recs), "saved": saved,
            "note": "POST /api/enrich to Walk/Transit-score these (paced separately)"}


@app.post("/api/import/csv")
async def import_csv_route(file: UploadFile, metro: str = "nyc", _=Depends(require_auth)):
    if metro not in METRO_KEYS:
        return {"error": f"metro must be one of {METRO_KEYS}"}
    text = (await file.read()).decode("utf-8-sig")
    return {"saved": import_csv(csv.DictReader(io.StringIO(text)), metro)}


def import_csv(rows, metro: str) -> int:
    saved = 0
    for i, row in enumerate(rows):
        rec: dict = {"metro": metro, "source": "csv"}
        for col, val in row.items():
            field = CSV_MAP.get((col or "").strip().lower())
            if not field or val in (None, ""):
                continue
            try:
                if field in _NUM:
                    rec[field] = int(float(str(val).replace(",", "").replace("$", "")))
                elif field in _FLOAT:
                    rec[field] = float(str(val).replace(",", "").replace("$", ""))
                else:
                    rec[field] = val
            except ValueError:
                continue
        if not rec.get("address"):
            continue
        rec.setdefault("source_url", f"csv://{metro}/{i}/{rec['address']}")
        db.save_listing(rec)
        saved += 1
    return saved
