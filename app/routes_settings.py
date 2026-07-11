"""Settings page — paste API keys / pick providers in the browser; saved to the
DB and applied live (no restart). Secrets are never rendered back; a blank field
keeps the stored value."""
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import settings_store
from .app import app, require_auth, templates
from .cache import budget_remaining_cents, spend_this_month
from .config import settings
from .settings_store import FIELDS


def _fields_ctx() -> list[dict]:
    out = []
    for name, label, kind in FIELDS:
        cur = getattr(settings, name, "")
        f = {"name": name, "label": label, "kind": kind, "secret": kind == "secret"}
        if kind.startswith("select:"):
            f["options"] = kind.split(":", 1)[1].split(",")
        if kind == "secret":
            f["is_set"] = bool(cur)
        else:
            f["value"] = cur
        out.append(f)
    return out


def _status() -> list[dict]:
    """What's active right now, so the user can see keys took effect."""
    from . import ai
    return [
        {"label": "Listings search (BM25)", "on": True, "note": "free, no key"},
        {"label": "Parcel data — NYC / Miami / LA / Chicago", "on": True, "note": "free, no key"},
        {"label": "Walk + Transit score (Overpass)", "on": True, "note": "free, no key"},
        {"label": "AI (parse / reply / extract / chat)", "on": ai.available(),
         "note": "needs ANTHROPIC_API_KEY (rules parser otherwise — understands far less)"},
        {"label": "Semantic ranking (Voyage)", "on": bool(settings.voyage_api_key),
         "note": "optional; BM25-only without it"},
        {"label": "Street View", "on": bool(settings.google_maps_key), "note": "optional"},
    ]


def _ctx(request: Request, saved: bool = False) -> dict:
    return {
        "request": request, "fields": _fields_ctx(), "status": _status(), "saved": saved,
        "spend_cents": spend_this_month(), "budget_cents": settings.monthly_budget_cents,
        "remaining_cents": budget_remaining_cents(),
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "settings.html", _ctx(request))


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, _=Depends(require_auth)):
    form = await request.form()
    updates: dict[str, str] = {}
    for name, _label, kind in FIELDS:
        v = (form.get(name) or "").strip()
        # selects always apply (blank = a valid "disabled" choice); for everything
        # else a blank field means "keep what's stored" (don't wipe keys/model/budget).
        if kind.startswith("select:") or kind == "bool":
            updates[name] = v
        elif v:
            updates[name] = v
    settings_store.save(updates)
    return templates.TemplateResponse(request, "settings.html", _ctx(request, saved=True))
