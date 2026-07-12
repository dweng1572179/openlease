"""The HTML surface. `/search` is the HTMX twin of `/api/search` — same pipeline, one
call, so the two can never drift."""
import json

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db
from .app import app, require_auth, spend_ctx, templates
from .models import METROS, to_api
from .routes_search import SearchRequest, api_search


@app.post("/search", response_class=HTMLResponse)
def search_fragment(request: Request, message: str = Form(...), metro: str = Form("nyc"),
                    session_id: str = Form(""), prior_state: str = Form(""),
                    _=Depends(require_auth)):
    body = SearchRequest(
        message=message, metro=metro,
        sessionId=session_id or None,
        priorState=json.loads(prior_state) if prior_state else None,
    )
    res = api_search(body, True)
    return templates.TemplateResponse(request, "_results.html", res)


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(request: Request, listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    if not row:
        return HTMLResponse("<p class='p-6'>No such listing.</p>", status_code=404)

    parcel = None
    if row.get("parcel_id"):
        parcel = db.get_parcel(row["parcel_id"])
    else:
        from . import registry
        prov = registry.parcel_provider(row["metro"])
        if prov:
            try:
                p = prov.lookup(row["address"], row.get("lat"), row.get("lng"))
            except Exception as e:  # noqa: BLE001 — a parcel API being down must not 500 the page
                import logging
                logging.getLogger("openlease").warning(
                    "parcel lookup failed for listing %s (%s): %s", listing_id, type(e).__name__, e)
                p = None
            if p:
                db.save_parcel(p)
                with db.get_conn() as conn:
                    conn.execute("UPDATE listing SET parcel_id = ? WHERE id = ?",
                                 (p.parcel_id, listing_id))
                parcel = db.get_parcel(p.parcel_id)

    return templates.TemplateResponse(
        request, "listing.html",
        {"l": to_api(row), "metro_meta": METROS[row["metro"]],
         "parcel": parcel, **spend_ctx()},
    )


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    return to_api(row) if row else {"error": "not found"}
