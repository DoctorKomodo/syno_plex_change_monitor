"""The async Engine: owns the watcher + pipeline and supports live rebuild.

Single event loop, no blocking calls in the loop. The only DB read happens via
``asyncio.to_thread(build_runtime_config, repo)`` at the loop boundary. The
watcher is injectable so non-Linux dev/tests can supply a fake backend.
"""

import asyncio
from datetime import UTC, datetime
from enum import StrEnum

import httpx
import structlog

from mediascanmonitor.config.runtime import RuntimeConfig, build_runtime_config
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.observ.events_bus import EventRecord, EventsBus, RawEventRecord, RawEventsBus
from mediascanmonitor.pipeline.debounce import Debouncer
from mediascanmonitor.pipeline.dispatcher import Dispatcher
from mediascanmonitor.pipeline.events import FsEvent, ScanRequest
from mediascanmonitor.pipeline.router import route
from mediascanmonitor.servers.base import ServerAdapter
from mediascanmonitor.servers.http import build_client
from mediascanmonitor.servers.registry import create_adapter
from mediascanmonitor.watcher.base import WatcherBackend
from mediascanmonitor.watcher.watch_limit import WatchLimitStatus, check_watch_limit

log = structlog.get_logger("engine")


class EngineState(StrEnum):
    starting = "starting"
    running = "running"
    blocked = "blocked"  # inotify gate not satisfied; watcher not attached
    stopped = "stopped"


class Engine:
    """Owns one watcher and the routing/debounce/dispatch pipeline."""

    def __init__(
        self,
        repo: Repo,
        *,
        watcher: WatcherBackend | None = None,
        events_bus: EventsBus | None = None,
        raw_events_bus: RawEventsBus | None = None,
    ) -> None:
        self._repo = repo
        self._watcher: WatcherBackend | None = watcher
        self._events_bus = events_bus
        self._raw_events_bus = raw_events_bus
        self._config: RuntimeConfig | None = None
        self._dispatcher: Dispatcher | None = None
        self._debouncer: Debouncer | None = None
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._started = False
        self._lock = asyncio.Lock()
        self.state: EngineState = EngineState.stopped
        self.watch_limit: WatchLimitStatus | None = None

    # -- public lifecycle ----------------------------------------------------

    async def start(self, *, park_when_blocked: bool = True) -> None:
        """Build the runtime, wire the pipeline, consult the inotify gate, consume events.

        The pipeline (adapters/dispatcher/debouncer/watcher) is wired **unconditionally** —
        none of it needs the gate and ``_build_adapters`` does no network I/O. Then:

        * ``park_when_blocked=True`` (web): a failed gate parks the watcher at **zero roots**
          and enters the consume loop anyway; the watcher yields nothing so the engine idles
          in ``blocked`` until a later ``rebuild()`` re-points the roots. The loop is never
          interrupted, so the web layer is never wedged (invariant 5).
        * ``park_when_blocked=False`` (headless): a failed gate sets ``blocked`` and **returns**
          without attaching roots or looping — the Bash-style block→exit-3 behavior. The
          headless test asserts ``watcher.roots_history == []``.
        """
        if self._started:
            raise RuntimeError("Engine.start() called more than once")
        self._started = True
        self.state = EngineState.starting

        config = await asyncio.to_thread(build_runtime_config, self._repo)
        self._config = config

        if self._watcher is None:
            # Lazy import keeps the engine importable on non-Linux dev machines.
            from mediascanmonitor.watcher.inotify_backend import InotifyBackend

            self._watcher = InotifyBackend(config.ignore_dirs)

        adapters, clients = await self._build_adapters(config)
        self._clients = clients
        self._dispatcher = Dispatcher(adapters)
        self._debouncer = Debouncer(self._dispatch, config.servers)

        gate_ok = await self._gate_ok(config)

        if not gate_ok and not park_when_blocked:
            # Headless: do NOT attach roots or loop; serve_headless observes `blocked` and exits 3.
            self.state = EngineState.blocked
            self._log_blocked(config)
            return

        self._watcher.set_roots(set(config.watch_paths) if gate_ok else set())
        self.state = EngineState.running if gate_ok else EngineState.blocked
        if gate_ok:
            log.info(
                "engine.started",
                watch_paths=sorted(config.watch_paths),
                servers=len(config.servers),
                routes=len(config.routes),
            )
        else:
            self._log_blocked(config)

        async for event in self._watcher.events():  # idles when roots are empty (parked)
            await self._handle_event(event)

    async def aclose(self) -> None:
        """Stop the event stream and release the watcher, debouncer, and clients."""
        if self._watcher is not None:
            await self._watcher.aclose()
        if self._debouncer is not None:
            await self._debouncer.aclose()
        for client in self._clients.values():
            await client.aclose()
        self._clients = {}
        self.state = EngineState.stopped
        log.info("engine.closed")

    async def _gate_ok(self, config: RuntimeConfig) -> bool:
        """Return whether the inotify gate is satisfied, and refresh ``self.watch_limit``.

        True when there is nothing to watch, OR the ``inotify_gate`` policy is ``off``, OR the
        measured watch limit is sufficient. Side effect: sets ``self.watch_limit`` (``None`` when
        there are no watch paths) so ``/api/status`` and ``/ready`` reflect live state. The repo
        read and the blocking ``check_watch_limit`` both run via ``asyncio.to_thread``.
        """
        if not config.watch_paths:
            self.watch_limit = None
            return True
        policy = await asyncio.to_thread(self._repo.get_setting, "inotify_gate")
        self.watch_limit = await asyncio.to_thread(
            check_watch_limit, config.watch_paths, config.ignore_dirs
        )
        if (policy or "enforce") == "off":
            return True
        return self.watch_limit.ok

    def _log_blocked(self, config: RuntimeConfig) -> None:
        wl = self.watch_limit
        log.error(
            "engine.blocked",
            reason="inotify watch limit too low",
            watch_paths=sorted(config.watch_paths),
            current=wl.current if wl else None,
            needed=wl.needed if wl else None,
            recommended=wl.recommended if wl else None,
            remediation=(
                f"raise fs.inotify.max_user_watches to >= {wl.recommended}" if wl else None
            ),
        )

    async def rebuild(self) -> None:
        """Rebuild the runtime snapshot and atomically swap it in — no restart.

        Re-evaluates the inotify gate and re-points the watcher roots, covering all four
        ``blocked ↔ running`` transitions **without raising**. The DB read (``to_thread``),
        the gate check (``to_thread``), and the old-client teardown (``aclose``) bracket a
        fully synchronous swap block, so no awaitable point splits a routing decision.
        """
        if (
            not self._started
            or self._dispatcher is None
            or self._debouncer is None
            or self._watcher is None
        ):
            raise RuntimeError("Engine.rebuild() called before start()")

        async with self._lock:
            new_config = await asyncio.to_thread(build_runtime_config, self._repo)
            new_adapters, new_clients = await self._build_adapters(new_config)
            gate_ok = await self._gate_ok(new_config)

            old_clients = self._clients
            old_paths = self._config.watch_paths if self._config else frozenset()
            new_paths = new_config.watch_paths
            roots = set(new_paths) if gate_ok else set()

            # --- atomic swap (NO await between these statements) -------------
            self._dispatcher.set_adapters(new_adapters)
            self._debouncer.update_servers(new_config.servers)
            self._config = new_config
            self._clients = new_clients
            self._watcher.set_roots(roots)
            self.state = EngineState.running if gate_ok else EngineState.blocked
            # ----------------------------------------------------------------

            log.info(
                "engine.rebuilt",
                gate_ok=gate_ok,
                state=self.state.value,
                added=sorted(new_paths - old_paths) if gate_ok else [],
                removed=sorted(old_paths - new_paths) if gate_ok else sorted(old_paths),
                servers=len(new_config.servers),
                routes=len(new_config.routes),
            )

            # Safe to await now: new requests already use new adapters/clients.
            for client in old_clients.values():
                await client.aclose()

    # -- internals -----------------------------------------------------------

    async def _build_adapters(
        self, config: RuntimeConfig
    ) -> tuple[dict[int, ServerAdapter], dict[int, httpx.AsyncClient]]:
        adapters: dict[int, ServerAdapter] = {}
        clients: dict[int, httpx.AsyncClient] = {}
        for server_id, server in config.servers.items():
            client = build_client(
                verify_tls=server.verify_tls, timeout_seconds=server.timeout_seconds
            )
            try:
                adapters[server_id] = create_adapter(server, client)
            except Exception as exc:  # isolate: one unsupported server never blocks the rest
                log.warning(
                    "engine.adapter_skipped",
                    server_id=server_id,
                    server_name=server.name,
                    server_type=server.type.value,
                    error=repr(exc),
                )
                await client.aclose()
                continue
            clients[server_id] = client
        return adapters, clients

    async def _dispatch(self, req: ScanRequest) -> None:
        """Adapter for the Debouncer's ``Awaitable[None]`` dispatch signature.

        Captures the dispatch outcome and, if an events bus is wired, publishes a
        redacted ``EventRecord`` (no secret field — invariant 1). Without a bus this
        reproduces the Phase 1/2 behavior exactly.
        """
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        result = await dispatcher.dispatch(req)
        bus = self._events_bus
        if bus is not None:
            bus.publish(
                EventRecord(
                    ts=datetime.now(UTC).isoformat(),
                    server_id=req.server_id,
                    server_name=req.server_name,
                    scan_mode=req.scan_mode.value,
                    scan_key=req.scan_key,
                    scan_path=req.scan_path,
                    library_id=req.library_id,
                    event_type=req.event_type.value,
                    file_path=req.file_path,
                    ok=result.ok,
                    status_code=result.status_code,
                    detail=result.detail,
                )
            )

    async def _handle_event(self, event: FsEvent) -> None:
        # Snapshot the immutable config locally BEFORE any await so a concurrent
        # rebuild swap never splits a single event across two configurations.
        config = self._config
        debouncer = self._debouncer
        if config is None or debouncer is None:
            return
        reqs = list(route(event, config))
        raw_bus = self._raw_events_bus
        if raw_bus is not None:
            raw_bus.publish(
                RawEventRecord(
                    ts=datetime.now(UTC).isoformat(),
                    event_type=event.event_type.value,
                    path=event.path,
                    is_dir=event.is_dir,
                    matched=bool(reqs),
                )
            )
        for req in reqs:
            await debouncer.submit(req)
