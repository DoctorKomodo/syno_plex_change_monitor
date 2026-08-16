"""Serve the web dashboard and the engine on one asyncio event loop (contract §J).

``serve_web`` builds the events bus, the engine (with the bus wired), bootstraps the
first-run password, builds the FastAPI app, and runs ``uvicorn.Server.serve`` and
``engine.start()`` concurrently. The engine *parks* if the inotify gate is blocked
(``park_when_blocked=True`` default) so the web layer always serves (invariant 5).
Shutdown (SIGINT/SIGTERM handled by uvicorn, or ``stop_event``) closes the engine and
cancels the start task. Returns process exit code ``0``.
"""

import asyncio
import contextlib

import structlog
import uvicorn

from mediascanmonitor.db.repo import Repo
from mediascanmonitor.engine import Engine
from mediascanmonitor.observ.events_bus import EventsBus, RawEventsBus
from mediascanmonitor.web.app import create_app
from mediascanmonitor.web.auth import bootstrap_password

log = structlog.get_logger("web.server")


async def serve_web(
    repo: Repo,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    session_secret: str,
    stop_event: asyncio.Event | None = None,
) -> int:
    stop = stop_event if stop_event is not None else asyncio.Event()

    bus = EventsBus()
    raw_bus = RawEventsBus()
    engine = Engine(repo, events_bus=bus, raw_events_bus=raw_bus)
    await asyncio.to_thread(bootstrap_password, repo)  # never logs the value
    app = create_app(repo, engine, bus, raw_bus, session_secret=session_secret)

    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)

    start_task = asyncio.create_task(engine.start())  # parks if the gate is blocked
    serve_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop.wait())
    log.info("web.serving", host=host, port=port)
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True  # ask uvicorn to wind down its accept loop
        await engine.aclose()  # closes watcher -> events() ends -> start_task returns
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
    log.info("web.stopped")
    return 0
