"""Shared test doubles and builders for engine/CLI tests (sub-plan 06).

Not collected by pytest (no ``test_`` prefix). Imported as ``tests._helpers``.
"""

import asyncio
from collections.abc import Callable, Iterable

import httpx

from mediascanmonitor.config.runtime import FolderRoute, RuntimeConfig, ServerRuntime
from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
)
from mediascanmonitor.pipeline.events import ScanRequest
from mediascanmonitor.servers.base import ServerAdapter, TestResult, TriggerResult

# FakeWatcher is the single canonical test watcher defined in sub-plan 04
# (`mediascanmonitor/watcher/base.py`); we import it here rather than redefine it.
# Its `emit()`/`current_roots`/`roots_history`/`closed` affordances cover these tests.
from mediascanmonitor.watcher.base import FakeWatcher

__all__ = [
    "FakeClient",
    "FakeWatcher",
    "RecordingAdapter",
    "make_config",
    "make_route",
    "make_server_runtime",
    "wait_for",
]


class FakeClient:
    """Stand-in for httpx.AsyncClient; only ``aclose`` is exercised by the engine."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class RecordingAdapter(ServerAdapter):
    """Adapter that records every ScanRequest it is asked to trigger."""

    server_type = ServerType.plex
    supported_scan_modes = frozenset({ScanMode.targeted, ScanMode.library})

    def __init__(self, server: ServerRuntime, client: httpx.AsyncClient) -> None:
        super().__init__(server, client)
        self.calls: list[ScanRequest] = []

    async def trigger(self, req: ScanRequest) -> TriggerResult:
        self.calls.append(req)
        return TriggerResult(ok=True, status_code=200, detail="ok")

    async def test(self) -> TestResult:
        return TestResult(ok=True, detail="ok")


def make_server_runtime(
    server_id: int,
    *,
    name: str,
    debounce: DebounceMode = DebounceMode.off,
    scan_mode: ScanMode = ScanMode.targeted,
) -> ServerRuntime:
    return ServerRuntime(
        server_id=server_id,
        name=name,
        type=ServerType.plex,
        base_url="http://plex.example:32400",
        verify_tls=True,
        timeout_seconds=10.0,
        secret="PLEX-TOKEN",
        scan_mode=scan_mode,
        debounce_mode=debounce,
        debounce_window_seconds=30,
        retry_attempts=3,
        webhook_method=None,
        webhook_headers_json=None,
        webhook_body_template=None,
        webhook_payload_preset=WebhookPreset.custom,
    )


def make_route(
    server_id: int,
    *,
    name: str,
    path: str,
    library_id: str,
    extensions: Iterable[str] = (),
    scan_mode: ScanMode = ScanMode.targeted,
    event_types: frozenset[FsEventType] = frozenset(FsEventType),
) -> FolderRoute:
    return FolderRoute(
        server_id=server_id,
        server_name=name,
        path=path,
        extensions=frozenset(extensions),
        library_id=library_id,
        scan_mode=scan_mode,
        event_types=event_types,
    )


def make_config(
    routes: list[FolderRoute],
    servers: list[ServerRuntime],
    *,
    ignore_dirs: frozenset[str] = frozenset({"@eaDir", "#snapshot"}),
) -> RuntimeConfig:
    return RuntimeConfig(
        watch_paths=frozenset(r.path for r in routes),
        routes=tuple(routes),
        servers={s.server_id: s for s in servers},
        ignore_dirs=ignore_dirs,
    )


async def wait_for(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll a predicate by yielding to the event loop until True or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition not satisfied within timeout")
