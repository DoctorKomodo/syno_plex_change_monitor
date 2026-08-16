"""Web test harness (shared by all Phase 3 sub-plans).

`repo` mirrors tests/db/conftest.py (real file-backed SQLite under tmp_path + a fresh
Fernet SecretBox). `engine` is a FakeEngine stub exposing the surface create_app/deps and
the routes touch (`.state`, `.watch_limit`, async `rebuild()`), so web tests never spin up
the real watcher/inotify. `client` is unauthenticated; `auth_client` has a real session
cookie obtained via POST /auth/login after a password is set; `aclient` is an httpx
AsyncClient over ASGITransport for the async SSE smoke (sub-plans 03/04).
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session
from starlette.testclient import TestClient

from mediascanmonitor.db.crypto import SecretBox
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.db.session import init_db, session_factory
from mediascanmonitor.engine import EngineState
from mediascanmonitor.observ.events_bus import EventsBus, RawEventsBus
from mediascanmonitor.watcher.watch_limit import WatchLimitStatus
from mediascanmonitor.web.app import create_app
from mediascanmonitor.web.auth import set_password

SESSION_SECRET = "test-secret-key"
AUTH_PASSWORD = "pw"


class FakeEngine:
    """Stand-in for engine.Engine: just the surface the web layer reads/calls."""

    def __init__(self) -> None:
        self.state: EngineState = EngineState.running
        self.watch_limit: WatchLimitStatus | None = None
        self.rebuild_calls = 0

    async def rebuild(self) -> None:
        self.rebuild_calls += 1


@pytest.fixture
def factory(tmp_path: Path) -> Callable[[], Session]:
    engine = init_db(tmp_path / "app.db")
    return session_factory(engine)


@pytest.fixture
def repo(factory: Callable[[], Session]) -> Repo:
    return Repo(factory, SecretBox(Fernet.generate_key()))


@pytest.fixture
def events_bus() -> EventsBus:
    return EventsBus()


@pytest.fixture
def raw_events_bus() -> RawEventsBus:
    return RawEventsBus()


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def app(  # type: ignore[no-untyped-def]
    repo: Repo, engine: FakeEngine, events_bus: EventsBus, raw_events_bus: RawEventsBus
):
    # FakeEngine is a structural stand-in for engine.Engine; create_app only stores it.
    return create_app(  # type: ignore[arg-type]
        repo, engine, events_bus, raw_events_bus, session_secret=SESSION_SECRET
    )


@pytest.fixture
def client(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app)


@pytest.fixture
def auth_client(app, repo: Repo) -> TestClient:  # type: ignore[no-untyped-def]
    set_password(repo, AUTH_PASSWORD)
    client = TestClient(app)
    resp = client.post("/auth/login", data={"password": AUTH_PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303, resp.text  # session cookie now set on `client`
    return client


@pytest.fixture
async def aclient(app, repo: Repo):  # type: ignore[no-untyped-def]
    # Authenticated async client for the SSE smoke (the stream route is behind require_page_auth).
    # httpx.AsyncClient defaults to follow_redirects=False, so the 303 returns and its Set-Cookie
    # persists in the client's cookie jar for subsequent stream reads.
    set_password(repo, AUTH_PASSWORD)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/auth/login", data={"password": AUTH_PASSWORD})
        assert resp.status_code == 303, resp.text  # session cookie now set on the async client
        yield c
