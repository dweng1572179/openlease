"""FastAPI app: single service, single user. Routes stay thin — data access in db.py,
provider calls behind registry.py. Auth is one password + a signed session cookie."""
import secrets

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .cache import budget_remaining_cents, spend_this_month
from .config import settings
from .db import init_db

app = FastAPI(title="OpenLease")

_secret = settings.secret_key or secrets.token_hex(32)
if not settings.secret_key:
    print("[openlease] no SECRET_KEY set — using an ephemeral one "
          "(sessions reset on restart). Set SECRET_KEY in .env to persist.")
if settings.openlease_password == "changeme":
    print("[openlease] WARNING: OPENLEASE_PASSWORD is still the default 'changeme' — "
          "set a real password in .env before exposing this beyond localhost.")
app.add_middleware(
    SessionMiddleware, secret_key=_secret, same_site="lax",
    https_only=settings.session_https_only,
)

templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def _startup():
    init_db()
    from . import settings_store
    settings_store.load_overrides()


# --- auth --------------------------------------------------------------------

class _Redirect(Exception):
    def __init__(self, to: str):
        self.to = to


def require_auth(request: Request):
    """Dependency: bounce unauthenticated requests to /login."""
    if not request.session.get("auth"):
        raise _Redirect("/login")
    return True


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.to, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, settings.openlease_password):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Wrong password."}, status_code=401
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- home --------------------------------------------------------------------

def spend_ctx() -> dict:
    return {
        "spend_cents": spend_this_month(),
        "budget_cents": settings.monthly_budget_cents,
        "remaining_cents": budget_remaining_cents(),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(
        "home.html", {"request": request, "metro": "nyc", **spend_ctx()}
    )


# Feature routes attach to `app` here as each task lands:
from . import routes_settings   # noqa: E402,F401  (T1)
