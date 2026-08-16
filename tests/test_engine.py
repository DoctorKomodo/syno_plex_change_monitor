"""Engine wiring, rebuild, atomicity, and shutdown tests (sub-plan 06)."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from mediascanmonitor import engine as engine_module
from mediascanmonitor.db.models import DebounceMode, ScanMode
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.engine import Engine, EngineState
from mediascanmonitor.observ.events_bus import EventsBus, RawEventsBus
from mediascanmonitor.pipeline.events import FsEvent, FsEventType
from mediascanmonitor.watcher.watch_limit import WatchLimitStatus
from tests._helpers import (
    FakeClient,
    FakeWatcher,
    RecordingAdapter,
    make_config,
    make_route,
    make_server_runtime,
    wait_for,
)


@pytest.fixture(autouse=True)
def _gate_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default: the inotify gate passes so start() attaches the watcher. The blocked/off
    # tests override check_watch_limit to report a shortfall.
    monkeypatch.setattr(
        engine_module,
        "check_watch_limit",
        lambda paths, ignore: WatchLimitStatus(
            current=1_000_000, dirs=0, needed=0, recommended=0, ok=True
        ),
    )


@pytest.fixture
def stub_repo() -> Repo:
    # build_runtime_config is monkeypatched in every test; start() reads only
    # get_setting("inotify_gate"), so the stub provides just that.
    class _StubRepo:
        def get_setting(self, key: str) -> str | None:
            return "enforce"

    return cast(Repo, _StubRepo())


def _patch_factories(monkeypatch: pytest.MonkeyPatch, created: dict[int, RecordingAdapter]) -> None:
    def fake_create_adapter(server: object, client: object) -> RecordingAdapter:
        adapter = RecordingAdapter(
            cast("engine_module.ServerRuntime", server),  # type: ignore[attr-defined]
            cast("engine_module.httpx.AsyncClient", client),  # type: ignore[attr-defined]
        )
        created[adapter.server.server_id] = adapter
        return adapter

    monkeypatch.setattr(engine_module, "create_adapter", fake_create_adapter)
    monkeypatch.setattr(engine_module, "build_client", lambda **_: FakeClient())


async def test_event_routes_to_adapter_trigger(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2", extensions={"mkv"})
    config = make_config([route], [server])
    monkeypatch.setattr(engine_module, "build_runtime_config", lambda repo: config)

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)

    # Pre-load one matching event and the end-of-stream sentinel, then run start()
    # to completion deterministically (no sleeps, no signals).
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/ep1.mkv", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()
    await engine.aclose()

    assert watcher.roots_history[0] == {"/data/tv"}
    assert set(created) == {1}
    calls = created[1].calls
    assert len(calls) == 1
    req = calls[0]
    assert req.server_id == 1
    assert req.scan_mode is ScanMode.targeted
    assert req.scan_path == "/data/tv/Shoresy"
    assert req.scan_key == "/data/tv/Shoresy"
    assert req.library_id == "2"
    assert req.top_folder == "Shoresy"


async def test_non_matching_extension_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2", extensions={"mkv"})
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/poster.jpg", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()
    await engine.aclose()

    assert created[1].calls == []


async def test_aclose_closes_watcher_debouncer_and_clients(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    clients: list[FakeClient] = []

    def fake_create_adapter(server: object, client: object) -> RecordingAdapter:
        adapter = RecordingAdapter(
            cast("engine_module.ServerRuntime", server),  # type: ignore[attr-defined]
            cast("engine_module.httpx.AsyncClient", client),  # type: ignore[attr-defined]
        )
        created[adapter.server.server_id] = adapter
        return adapter

    def fake_build_client(**_: object) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_module, "create_adapter", fake_create_adapter)
    monkeypatch.setattr(engine_module, "build_client", fake_build_client)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    start_task = asyncio.create_task(engine.start())
    await wait_for(lambda: bool(watcher.roots_history))

    await engine.aclose()
    await start_task  # watcher closed -> events() ends -> start() returns

    assert watcher.closed is True
    assert clients and all(c.closed for c in clients)


async def test_blocked_when_watch_limit_insufficient_and_enforced(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(
        engine_module,
        "check_watch_limit",
        lambda paths, ignore: WatchLimitStatus(
            current=10, dirs=100, needed=120, recommended=144, ok=False
        ),
    )

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    await engine.start(park_when_blocked=False)  # headless contract: block -> return, no roots

    assert engine.state is EngineState.blocked
    assert engine.watch_limit is not None and engine.watch_limit.ok is False
    assert watcher.roots_history == []  # gate blocked before set_roots
    await engine.aclose()


async def test_gate_off_attaches_despite_insufficient_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(
        engine_module,
        "check_watch_limit",
        lambda paths, ignore: WatchLimitStatus(
            current=10, dirs=100, needed=120, recommended=144, ok=False
        ),
    )

    class _OffRepo:
        def get_setting(self, key: str) -> str | None:
            return "off"

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    watcher = FakeWatcher()
    engine = Engine(cast(Repo, _OffRepo()), watcher=watcher)
    await watcher.aclose()  # end the event stream immediately
    await engine.start()

    assert engine.state is EngineState.running  # attached despite not-ok limit (gate off)
    assert watcher.roots_history == [{"/data/tv"}]
    await engine.aclose()


async def test_unsupported_server_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}

    def fake_create_adapter(server: object, client: object) -> RecordingAdapter:
        sr = cast("engine_module.ServerRuntime", server)  # type: ignore[attr-defined]
        if sr.server_id == 2:
            raise ValueError("no adapter registered for type 'emby'")
        adapter = RecordingAdapter(
            sr,
            cast("engine_module.httpx.AsyncClient", client),  # type: ignore[attr-defined]
        )
        created[sr.server_id] = adapter
        return adapter

    monkeypatch.setattr(engine_module, "create_adapter", fake_create_adapter)
    monkeypatch.setattr(engine_module, "build_client", lambda **_: FakeClient())

    s1 = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    s2 = make_server_runtime(2, name="emby", debounce=DebounceMode.off)
    r1 = make_route(1, name="plex", path="/data/tv", library_id="2")
    r2 = make_route(2, name="emby", path="/data/tv", library_id="5")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([r1, r2], [s1, s2])
    )

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    await watcher.emit(
        FsEvent(path="/data/tv/Show/ep.mkv", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()  # must NOT raise despite server 2's adapter construction failing

    assert engine.state is EngineState.running
    assert set(created) == {1}  # server 2 skipped; server 1 built
    assert len(created[1].calls) == 1  # server 1 still dispatches
    await engine.aclose()


async def test_rebuild_adds_then_removes_roots_and_reroutes(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    tv = make_route(1, name="plex", path="/data/tv", library_id="2")
    movies = make_route(1, name="plex", path="/data/movies", library_id="1")

    holder = SimpleNamespace(current=make_config([tv], [server]))
    monkeypatch.setattr(engine_module, "build_runtime_config", lambda repo: holder.current)

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    start_task = asyncio.create_task(engine.start())
    await wait_for(lambda: bool(watcher.roots_history))
    assert watcher.current_roots == {"/data/tv"}

    # --- add a second folder, rebuild -> union grows ---
    holder.current = make_config([tv, movies], [server])
    await engine.rebuild()
    assert watcher.current_roots == {"/data/tv", "/data/movies"}

    await watcher.emit(
        FsEvent(path="/data/movies/Dune/d.mkv", event_type=FsEventType.created, is_dir=False)
    )
    await wait_for(lambda: any(c.scan_path == "/data/movies/Dune" for c in created[1].calls))

    # --- remove the original folder, rebuild -> root drops, no more routing there ---
    holder.current = make_config([movies], [server])
    await engine.rebuild()
    assert watcher.current_roots == {"/data/movies"}

    before = len(created[1].calls)
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/ep1.mkv", event_type=FsEventType.created, is_dir=False)
    )
    # Give the loop ample chances; the removed root must produce NO new dispatch.
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(created[1].calls) == before

    await engine.aclose()
    await start_task


async def test_rebuild_closes_old_clients_and_swaps_adapters(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: list[RecordingAdapter] = []
    clients: list[FakeClient] = []

    def fake_create_adapter(server: object, client: object) -> RecordingAdapter:
        adapter = RecordingAdapter(
            cast("engine_module.ServerRuntime", server),  # type: ignore[attr-defined]
            cast("engine_module.httpx.AsyncClient", client),  # type: ignore[attr-defined]
        )
        created.append(adapter)
        return adapter

    def fake_build_client(**_: object) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(engine_module, "create_adapter", fake_create_adapter)
    monkeypatch.setattr(engine_module, "build_client", fake_build_client)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    tv = make_route(1, name="plex", path="/data/tv", library_id="2")
    holder = SimpleNamespace(current=make_config([tv], [server]))
    monkeypatch.setattr(engine_module, "build_runtime_config", lambda repo: holder.current)

    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher)
    start_task = asyncio.create_task(engine.start())
    await wait_for(lambda: bool(watcher.roots_history))

    assert len(clients) == 1  # one client built at start
    await engine.rebuild()
    assert len(clients) == 2  # a fresh client built on rebuild
    assert clients[0].closed is True  # the OLD client was closed after the swap
    assert clients[1].closed is False
    assert len(created) == 2  # adapters rebuilt

    await engine.aclose()
    await start_task
    assert clients[1].closed is True


async def test_rebuild_before_start_raises(stub_repo: Repo) -> None:
    engine = Engine(stub_repo, watcher=FakeWatcher())
    with pytest.raises(RuntimeError, match="before start"):
        await engine.rebuild()


async def test_dispatch_publishes_event_record_when_bus_present(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2", extensions={"mkv"})
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    bus = EventsBus()
    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher, events_bus=bus)
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/ep1.mkv", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()
    await engine.aclose()

    records = bus.recent()
    assert len(records) == 1
    rec = records[0]
    assert rec.server_id == 1
    assert rec.server_name == "plex"
    assert rec.scan_mode == ScanMode.targeted.value
    assert rec.scan_key == "/data/tv/Shoresy"
    assert rec.scan_path == "/data/tv/Shoresy"
    assert rec.library_id == "2"
    assert rec.event_type == FsEventType.created.value
    assert rec.file_path == "/data/tv/Shoresy/ep1.mkv"
    assert rec.ok is True
    assert rec.status_code == 200
    assert rec.ts.endswith("+00:00")  # ISO-8601 UTC, timezone-aware


async def test_handle_event_publishes_raw_record_matched(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2", extensions={"mkv"})
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    raw_bus = RawEventsBus()
    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher, raw_events_bus=raw_bus)
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/ep1.mkv", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()
    await engine.aclose()

    records = raw_bus.recent()
    assert len(records) == 1
    rec = records[0]
    assert rec.event_type == FsEventType.created.value
    assert rec.path == "/data/tv/Shoresy/ep1.mkv"
    assert rec.is_dir is False
    assert rec.matched is True
    assert rec.ts.endswith("+00:00")  # ISO-8601 UTC, timezone-aware


async def test_handle_event_publishes_raw_record_unmatched(
    monkeypatch: pytest.MonkeyPatch, stub_repo: Repo
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2", extensions={"mkv"})
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    raw_bus = RawEventsBus()
    watcher = FakeWatcher()
    engine = Engine(stub_repo, watcher=watcher, raw_events_bus=raw_bus)
    await watcher.emit(
        FsEvent(path="/data/tv/Shoresy/poster.jpg", event_type=FsEventType.created, is_dir=False)
    )
    await watcher.aclose()
    await engine.start()
    await engine.aclose()

    records = raw_bus.recent()
    assert len(records) == 1
    assert records[0].matched is False
    assert created[1].calls == []  # unmatched: never reached the adapter


# ---------------------------------------------------------------------------
# Gate-recovery transition tests (blocked ↔ running, no restart)
# ---------------------------------------------------------------------------


class _MutRepo:
    """Repo stub with a settable inotify_gate policy (start/rebuild read only get_setting)."""

    def __init__(self, gate: str = "enforce") -> None:
        self.gate = gate

    def get_setting(self, key: str) -> str | None:
        return self.gate


def _ok(_paths: object, _ignore: object) -> WatchLimitStatus:
    return WatchLimitStatus(current=1_000_000, dirs=0, needed=0, recommended=0, ok=True)


def _not_ok(_paths: object, _ignore: object) -> WatchLimitStatus:
    return WatchLimitStatus(current=10, dirs=100, needed=120, recommended=144, ok=False)


async def test_blocked_to_running_on_policy_flip_to_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(engine_module, "check_watch_limit", _not_ok)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    repo = _MutRepo(gate="enforce")
    watcher = FakeWatcher()
    engine = Engine(cast(Repo, repo), watcher=watcher)
    start_task = asyncio.create_task(engine.start())  # default park_when_blocked=True
    await wait_for(lambda: engine.state is EngineState.blocked)
    assert watcher.current_roots == set()  # parked at zero roots, loop alive

    # Flip the policy to off and rebuild -> gate now passes -> roots attach, state running.
    repo.gate = "off"
    await engine.rebuild()
    assert engine.state is EngineState.running
    assert watcher.current_roots == {"/data/tv"}

    await engine.aclose()
    await start_task


async def test_blocked_to_running_on_limit_raised_then_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(engine_module, "check_watch_limit", _not_ok)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    repo = _MutRepo(gate="enforce")
    watcher = FakeWatcher()
    engine = Engine(cast(Repo, repo), watcher=watcher)
    start_task = asyncio.create_task(engine.start())
    await wait_for(lambda: engine.state is EngineState.blocked)

    # Operator raised fs.inotify.max_user_watches out of band -> recheck re-evaluates the gate.
    monkeypatch.setattr(engine_module, "check_watch_limit", _ok)
    await engine.rebuild()
    assert engine.state is EngineState.running
    assert watcher.current_roots == {"/data/tv"}
    assert engine.watch_limit is not None and engine.watch_limit.ok is True

    await engine.aclose()
    await start_task


async def test_running_to_blocked_on_config_outgrowth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(engine_module, "check_watch_limit", _ok)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    repo = _MutRepo(gate="enforce")
    watcher = FakeWatcher()
    engine = Engine(cast(Repo, repo), watcher=watcher)
    start_task = asyncio.create_task(engine.start())
    await wait_for(lambda: engine.state is EngineState.running)
    assert watcher.current_roots == {"/data/tv"}

    # The new config outgrows the limit under enforce -> rebuild parks at zero roots.
    monkeypatch.setattr(engine_module, "check_watch_limit", _not_ok)
    await engine.rebuild()
    assert engine.state is EngineState.blocked
    assert watcher.current_roots == set()

    await engine.aclose()
    await start_task


async def test_headless_park_false_blocked_returns_without_attaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[int, RecordingAdapter] = {}
    _patch_factories(monkeypatch, created)
    monkeypatch.setattr(engine_module, "check_watch_limit", _not_ok)

    server = make_server_runtime(1, name="plex", debounce=DebounceMode.off)
    route = make_route(1, name="plex", path="/data/tv", library_id="2")
    monkeypatch.setattr(
        engine_module, "build_runtime_config", lambda repo: make_config([route], [server])
    )

    watcher = FakeWatcher()
    engine = Engine(cast(Repo, _MutRepo(gate="enforce")), watcher=watcher)
    await engine.start(park_when_blocked=False)  # returns immediately, no loop

    assert engine.state is EngineState.blocked
    assert watcher.roots_history == []  # set_roots never called -> headless exit-3 contract
    await engine.aclose()
