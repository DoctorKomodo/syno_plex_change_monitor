"""FastAPI application factory (contract §A).

PURE: takes the already-built Repo / Engine / EventsBus, stores them on app.state, mounts
SessionMiddleware (signs the cookie with itsdangerous; same_site="lax" is the deliberate
CSRF defense — see contract §C), Jinja2 templates, the LoginRateLimiter, and every router,
then returns the app. No I/O, no env reads, no password bootstrap (serve_web does that,
sub-plan 03), so each test builds its own app cheaply.

The session stores exactly one key: "authed" (True once logged in). Later sub-plans append
their include_router(...) lines below — keep every line on merge (the one shared merge
point across Phase 3 sub-plans).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from mediascanmonitor.config.defaults import EXTENSION_PRESETS
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.engine import Engine
from mediascanmonitor.observ.events_bus import EventsBus, RawEventsBus
from mediascanmonitor.web import auth
from mediascanmonitor.web.api import events as api_events
from mediascanmonitor.web.api import folders as api_folders
from mediascanmonitor.web.api import servers as api_servers
from mediascanmonitor.web.api import system as system_api
from mediascanmonitor.web.pages import router as pages_router
from mediascanmonitor.web.ratelimit import LoginRateLimiter

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(
    repo: Repo,
    engine: Engine,
    events_bus: EventsBus,
    raw_events_bus: RawEventsBus,
    *,
    session_secret: str,
) -> FastAPI:
    app = FastAPI(title="media-scan-monitor")

    app.state.repo = repo
    app.state.engine = engine
    app.state.events_bus = events_bus
    app.state.raw_events_bus = raw_events_bus
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Offered to the folder editor's extension chip-picker (see _folder_rows_script.html).
    app.state.templates.env.globals["extension_presets"] = EXTENSION_PRESETS
    app.state.limiter = LoginRateLimiter()
    # A SECOND limiter, dedicated to the unauthenticated /auth/reset-password POST, kept
    # separate from the login limiter so reset attempts never trip the login lockout (and
    # vice-versa). Stricter: 3 resets per hour per IP.
    app.state.reset_limiter = LoginRateLimiter(max_attempts=3, window_seconds=3600.0)

    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=False,
    )

    app.include_router(auth.router)
    app.include_router(api_servers.router)
    app.include_router(api_folders.router)
    app.include_router(api_events.router)
    app.include_router(system_api.health_router)
    app.include_router(system_api.router)
    app.include_router(pages_router)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
