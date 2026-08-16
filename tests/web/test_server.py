"""serve_web lifecycle — uvicorn + engine on one loop, stopped via stop_event (no real bind)."""

import asyncio

import pytest
import uvicorn

from mediascanmonitor import web as _web_pkg  # noqa: F401  (ensure package import path)
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.engine import EngineState
from mediascanmonitor.web import server as server_module


async def test_serve_web_starts_engine_and_shuts_down_cleanly(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    created: dict[str, _FakeEngine] = {}

    class _FakeEngine:
        def __init__(
            self,
            repo: Repo,
            *,
            events_bus: object | None = None,
            raw_events_bus: object | None = None,
        ) -> None:
            self.closed = False
            self.started = False
            self.state = EngineState.running
            self.watch_limit = None

        async def start(self, *, park_when_blocked: bool = True) -> None:
            self.started = True
            await asyncio.Event().wait()  # block until the task is cancelled at shutdown

        async def aclose(self) -> None:
            self.closed = True

    def make_engine(
        repo: Repo, *, events_bus: object | None = None, raw_events_bus: object | None = None
    ) -> _FakeEngine:
        eng = _FakeEngine(repo, events_bus=events_bus, raw_events_bus=raw_events_bus)
        created["engine"] = eng
        return eng

    monkeypatch.setattr(server_module, "Engine", make_engine)
    monkeypatch.setattr(server_module, "bootstrap_password", lambda repo: None)

    stop = asyncio.Event()

    async def fake_serve(self: uvicorn.Server) -> None:
        await stop.wait()  # stand in for "serve until shutdown"; never binds a socket

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)

    stop.set()  # request shutdown immediately
    code = await server_module.serve_web(repo, session_secret="x" * 32, stop_event=stop)

    assert code == 0
    assert created["engine"].started is True
    assert created["engine"].closed is True  # engine.aclose() ran on shutdown
