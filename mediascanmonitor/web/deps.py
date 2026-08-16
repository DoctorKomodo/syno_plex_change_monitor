"""FastAPI dependency providers + auth guards (contract §B).

State accessors read what create_app stored on app.state (sync; no I/O). The two guards
enforce the allow-list: an API route returns JSON 401, an HTML page 303-redirects to
/login (or /setup when no password is set yet, which is what makes first-run setup
reachable while everything else is locked). `is_password_set` is a sync Repo read, so the
guards call it via asyncio.to_thread (cross-plan invariant 3).
"""

import asyncio

from fastapi import HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from mediascanmonitor.db.repo import Repo
from mediascanmonitor.engine import Engine
from mediascanmonitor.observ.events_bus import EventsBus, RawEventsBus

# Import the auth MODULE, not the `is_password_set` symbol: web/app.py imports auth, auth's
# router imports this deps module, and a `from …auth import is_password_set` here would bind a
# name that auth has not defined yet (partially-initialized module → ImportError). Module-attribute
# access (`auth.is_password_set`) is resolved lazily at call time, so it breaks the cycle.
from mediascanmonitor.web import auth


def get_repo(request: Request) -> Repo:
    repo: Repo = request.app.state.repo
    return repo


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_events_bus(request: Request) -> EventsBus:
    bus: EventsBus = request.app.state.events_bus
    return bus


def get_raw_events_bus(request: Request) -> RawEventsBus:
    bus: RawEventsBus = request.app.state.raw_events_bus
    return bus


def get_templates(request: Request) -> Jinja2Templates:
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _is_authed(request: Request) -> bool:
    return request.session.get("authed") is True


# Paths an authenticated but must-change session may still reach (everything else
# page-redirects to /account/password until the bootstrap password is rotated).
# Matched as a whole path or a sub-path (prefix + "/"), never a bare string prefix, so a
# future sibling like /account/password-reset is NOT accidentally allowlisted.
_MUST_CHANGE_ALLOWLIST = ("/account/password", "/auth/logout")


def _on_must_change_allowlist(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _MUST_CHANGE_ALLOWLIST)


async def require_api_auth(request: Request) -> None:
    repo = get_repo(request)
    if _is_authed(request):
        if await asyncio.to_thread(auth.is_must_change, repo):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="password change required"
            )
        return
    if not await asyncio.to_thread(auth.is_password_set, repo):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="setup required")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


async def require_page_auth(request: Request) -> None:
    repo = get_repo(request)
    if _is_authed(request):
        if await asyncio.to_thread(auth.is_must_change, repo) and not _on_must_change_allowlist(
            request.url.path
        ):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/account/password"},
            )
        return
    location = "/login"
    if not await asyncio.to_thread(auth.is_password_set, repo):
        location = "/setup"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": location},
    )
