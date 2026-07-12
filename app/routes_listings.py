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
    return templates.TemplateResponse(
        request, "listing.html",
        {"l": to_api(row), "metro_meta": METROS[row["metro"]],
         "parcel": None, **spend_ctx()},   # T9 fills `parcel`
    )


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    return to_api(row) if row else {"error": "not found"}
