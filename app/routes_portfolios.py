"""Saves + client shortlists."""
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import db
from .app import app, require_auth, spend_ctx, templates
from .models import to_api


@app.post("/listings/{listing_id}/save", response_class=HTMLResponse)
def save_toggle(listing_id: int, _=Depends(require_auth)):
    on = db.toggle_save(listing_id)
    return HTMLResponse(
        f'<button hx-post="/listings/{listing_id}/save" hx-swap="outerHTML" '
        f'class="rounded border px-3 py-1 text-xs '
        f'{"bg-sky-600 text-white" if on else "text-slate-600"}">'
        f'{"Saved" if on else "Save"}</button>'
    )


@app.get("/portfolios", response_class=HTMLResponse)
def portfolios_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "portfolios.html", {
        "portfolios": db.list_portfolios(),
        "saved": [to_api(r) for r in db.list_saved()],
        **spend_ctx(),
    })


@app.post("/portfolios", response_class=HTMLResponse)
def portfolio_create(request: Request, name: str = Form(...), _=Depends(require_auth)):
    db.create_portfolio(name)
    return portfolios_page(request, True)


@app.post("/portfolios/{portfolio_id}/add")
def portfolio_add(portfolio_id: int, listing_id: int = Form(...), _=Depends(require_auth)):
    db.add_to_portfolio(portfolio_id, listing_id)
    return {"ok": True}
