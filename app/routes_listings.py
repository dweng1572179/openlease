"""The HTML surface. `/search` is the HTMX twin of `/api/search` — same pipeline, one
call, so the two can never drift."""
import json

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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
    from . import ai
    row = db.get_listing(listing_id)
    if not row:
        return HTMLResponse("<p class='p-6'>No such listing.</p>", status_code=404)

    # Highlights are generated ONCE from the facts and cached on the row itself (T13):
    # a listing that already has highlights_json never re-asks the model. Keyless, or on
    # any AI failure, ai.highlights() returns None and the page simply shows none (the
    # template surfaces the keyless case honestly via `ai_available` below) — never a
    # crash, never invented text.
    if not row.get("highlights_json"):
        hl = ai.highlights(row)
        if hl:
            with db.get_conn() as conn:
                conn.execute("UPDATE listing SET highlights_json = ? WHERE id = ?",
                             (json.dumps(hl), listing_id))
            row["highlights_json"] = json.dumps(hl)

    parcel = None
    if row.get("parcel_id"):
        parcel = db.get_parcel(row["parcel_id"])
    else:
        from . import registry
        prov = registry.parcel_provider(row["metro"])
        if prov:
            import logging
            log = logging.getLogger("openlease")
            try:
                p = prov.lookup(row["address"], row.get("lat"), row.get("lng"))
            except Exception as e:  # noqa: BLE001 — a parcel API being down must not 500 the page
                log.warning(
                    "parcel lookup failed for listing %s (%s): %s", listing_id, type(e).__name__, e)
                p = None
            if p is None:
                # A clean "no match" is a lookup FAILURE, not a structural null — but it
                # renders identically ("No parcel matched this address"). Unlogged, a metro
                # whose address-search field has drifted to zero matches looks exactly like
                # a normal page load, and we'd never notice the whole metro had gone dark.
                log.warning(
                    "parcel lookup returned NO MATCH for listing %s (%s): %r — if this is "
                    "every listing in this metro, the provider's address query has drifted",
                    listing_id, row["metro"], row["address"])
            if p:
                db.save_parcel(p)
                with db.get_conn() as conn:
                    conn.execute("UPDATE listing SET parcel_id = ? WHERE id = ?",
                                 (p.parcel_id, listing_id))
                parcel = db.get_parcel(p.parcel_id)

    return templates.TemplateResponse(
        request, "listing.html",
        {"l": to_api(row), "metro_meta": METROS[row["metro"]],
         "parcel": parcel, "saved": db.is_saved(listing_id),
         "history": db.chat_history(listing_id), "listing_id": listing_id,
         "portfolios": db.list_portfolios(), "ai_available": ai.available(), **spend_ctx()},
    )


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, _=Depends(require_auth)):
    row = db.get_listing(listing_id)
    return to_api(row) if row else {"error": "not found"}


# --- per-listing RAG chat (T13) ------------------------------------------------

class AskBody(BaseModel):
    question: str
    history: list[dict] = []


@app.post("/api/listings/{listing_id}/ask")
def api_ask(listing_id: int, body: AskBody, _=Depends(require_auth)):
    from . import ai
    row = db.get_listing(listing_id)
    if not row:
        return {"error": "not found"}
    history = body.history or db.chat_history(listing_id)
    answer = ai.ask(row, body.question, history)
    db.add_chat(listing_id, "user", body.question)
    db.add_chat(listing_id, "assistant", answer)
    return {"answer": answer, "history": db.chat_history(listing_id)}


@app.post("/listings/{listing_id}/ask", response_class=HTMLResponse)
def ask_fragment(request: Request, listing_id: int, question: str = Form(...),
                 _=Depends(require_auth)):
    from . import ai
    api_ask(listing_id, AskBody(question=question), True)
    return templates.TemplateResponse(
        request, "_chat.html", {"listing_id": listing_id, "ai_available": ai.available(),
                                "history": db.chat_history(listing_id)})
