"""Free NYC government supply — zero ToS surface, and one thing no broker feed has: a
VACANCY FLAG on every ground- and second-floor commercial space in the city.

  Storefront Registry (Socrata 92iy-9c3n, keyless) — address, BBL, lat/lng, business
    activity, and `vacant_on_12_31`. A vacancy is a lead.
  ACRIS (keyless) — deeds and mortgages with amounts and dates. A big mortgage recorded
    against a building with a vacant storefront is a distress signal.

Verified live 2026-07-12 — the plan's field names/join key DRIFTED on both endpoints (the
exact failure Task 9 found on all four metros' parcel data; see
docs/implementation-plan.md Task 11 correction for the full write-up):

  1. `92iy-9c3n` has no `primary_business_address`, `street_number`, or `street_name`
     column. The real columns are `property_street_address_or` (the pre-joined full
     address, e.g. "271 BROAD STREET") and, as a fallback, `property_number` +
     `property_street`. Run against the field names the plan guessed, every real row's
     address came back "" and was silently dropped — zero storefronts, not five.
  2. `bnx9-e6tj` ("ACRIS - Real Property Master") has NO borough/block/lot columns at
     all — querying it that way is a 400 ("Unrecognized arguments"), not an empty list.
     ACRIS is split across datasets: `8h5j-fqxa` ("ACRIS - Real Property Legals") holds
     the borough/block/lot -> document_id join; `bnx9-e6tj` holds
     document_id -> doc_type/amount/date. A BBL-to-signal lookup needs both, in sequence.
"""
import logging

import httpx

from ..cache import cached

log = logging.getLogger("openlease")

STOREFRONT = "https://data.cityofnewyork.us/resource/92iy-9c3n.json"
ACRIS_LEGALS = "https://data.cityofnewyork.us/resource/8h5j-fqxa.json"   # bbl -> document_id
ACRIS = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"          # document_id -> doc


PAGE = 5000        # Socrata serves this comfortably; 43,978 rows is 9 pages


def storefronts(limit: int = 50_000, vacant_only: bool = True) -> list[dict]:
    """Every vacant storefront the City publishes, as Listing dicts.

    Two things were quietly costing us most of this feed.

    The default limit was 500 — of 43,978. The registry is the single largest supply of
    NYC ground-floor retail in existence, it is published by the city, and no broker site
    has it. Stopping at 500 made the app look shallow for no reason at all.

    And `source_url` was keyed on the BBL alone. A BBL is a TAX LOT — one building — so a
    building with six vacant storefronts collapsed into ONE row on upsert, and we silently
    lost ~30% of every page we did fetch (500 fetched -> 350 saved). Socrata's `:id` is a
    stable per-ROW key, which is what a storefront actually is.
    """
    where = "vacant_on_12_31='YES'" if vacant_only else "1=1"

    def fetch():
        got: list[dict] = []
        while len(got) < limit:
            page = min(PAGE, limit - len(got))
            r = httpx.get(STOREFRONT, params={
                "$select": ":id,bbl,borough,latitude,longitude,property_street_address_or,"
                           "property_number,property_street,primary_business_activity",
                "$where": where, "$limit": page, "$offset": len(got),
                "$order": ":id",     # a stable order, or paging can repeat and skip rows
            }, timeout=120.0)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            got.extend(batch)
            log.info("nyc storefronts: %d fetched", len(got))
            if len(batch) < page:
                break            # short page = the end of the registry
        return got

    rows = cached("nyc_storefront", "query", {"where": where, "limit": limit}, fetch)
    out = []
    for r in rows:
        bbl = r.get("bbl")
        addr = r.get("property_street_address_or") or (
            f"{r.get('property_number', '')} {r.get('property_street', '')}".strip())
        lat, lng = r.get("latitude"), r.get("longitude")
        if not addr or not bbl:
            continue
        out.append({
            "source": "nyc_storefront",
            # Keyed on the ROW, not the BBL: six vacant storefronts in one building are six
            # storefronts, and upserting them onto one tax-lot URL threw five of them away.
            "source_url": (f"https://data.cityofnewyork.us/resource/92iy-9c3n.json"
                           f"?$where=:id='{r.get(':id')}'"),
            "metro": "nyc",
            "status": "available",
            "address": addr,
            # the dataset's own borough names come back ALL CAPS ("STATEN ISLAND"); the
            # rest of the app (metros.yml, the hard borough filter) uses Title Case
            # ("Staten Island") — normalize here or a borough filter can never match.
            "borough": (r.get("borough") or "").title() or None,
            "lat": float(lat) if lat else None,
            "lng": float(lng) if lng else None,
            "property_type": "retail",
            "transaction_type": "lease",
            "parcel_id": f"nyc:{bbl}",
            "our_description": (
                f"Vacant ground-floor commercial space at {addr}, from the City of New York's "
                f"Storefront Registry (last reported use: "
                f"{r.get('primary_business_activity') or 'not stated'}). No broker is attached — "
                f"this is a vacancy lead, not a listing."
            ),
        })
    return out


def acris_signals(bbl: str) -> list[dict]:
    """Deeds/mortgages recorded against a BBL. A large recent mortgage under a vacant
    storefront is the distress signal worth a call.

    Two Socrata calls, not one: `8h5j-fqxa` (Legals) maps borough/block/lot -> the
    document_ids recorded against that lot; `bnx9-e6tj` (Master) maps those document_ids
    to doc_type/amount/date. Master alone has no BBL column to query by."""
    if not bbl or len(bbl) < 10:
        return []
    borough, block, lot = bbl[0], int(bbl[1:6]), int(bbl[6:10])

    def fetch_legals():
        r = httpx.get(ACRIS_LEGALS, params={
            "borough": borough, "block": block, "lot": lot, "$limit": 200}, timeout=60.0)
        r.raise_for_status()
        return r.json()

    legals = cached("acris_legals", "bbl", {"bbl": bbl}, fetch_legals)
    doc_ids = sorted({r["document_id"] for r in legals if r.get("document_id")})
    if not doc_ids:
        return []   # legitimate -- not every parcel has ACRIS history; never fire Master

    def fetch_master():
        where = "document_id in(" + ",".join(f"'{d}'" for d in doc_ids) + ")"
        r = httpx.get(ACRIS, params={
            "$where": where, "$order": "recorded_datetime DESC", "$limit": 20}, timeout=60.0)
        r.raise_for_status()
        return r.json()

    rows = cached("acris_master", "doc_ids", {"doc_ids": doc_ids}, fetch_master)
    return [{"doc_type": r.get("doc_type"), "amount": r.get("document_amt"),
             "date": r.get("recorded_datetime")} for r in rows]
