"""Tests for config/runtime.py — runtime snapshot dataclasses + builder."""

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from mediascanmonitor.config.runtime import (
    FolderRoute,
    RuntimeConfig,
    ServerRuntime,
    build_runtime_config,
)
from mediascanmonitor.db.models import (
    DebounceMode,
    FileType,
    Folder,
    FsEventType,
    ScanMode,
    Server,
    ServerType,
    WebhookPreset,
)

if TYPE_CHECKING:
    from mediascanmonitor.db.repo import Repo


def test_server_runtime_fields_frozen_slotted() -> None:
    sr = ServerRuntime(
        server_id=1,
        name="plex-main",
        type=ServerType.plex,
        base_url="https://plex.local:32400",
        verify_tls=True,
        timeout_seconds=10.0,
        secret="token-abc",
        scan_mode=ScanMode.targeted,
        debounce_mode=DebounceMode.trailing,
        debounce_window_seconds=30,
        retry_attempts=3,
        webhook_method=None,
        webhook_headers_json=None,
        webhook_body_template=None,
        webhook_payload_preset=WebhookPreset.custom,
    )
    assert sr.server_id == 1
    assert sr.secret == "token-abc"
    assert sr.type is ServerType.plex
    assert not hasattr(sr, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        sr.secret = "leak"  # type: ignore[misc]


def test_server_runtime_secret_excluded_from_repr() -> None:
    sr = ServerRuntime(
        server_id=1,
        name="plex-main",
        type=ServerType.plex,
        base_url="",
        verify_tls=True,
        timeout_seconds=10.0,
        secret="super-secret-token",
        scan_mode=ScanMode.targeted,
        debounce_mode=DebounceMode.trailing,
        debounce_window_seconds=30,
        retry_attempts=3,
        webhook_method=None,
        webhook_headers_json=None,
        webhook_body_template=None,
        webhook_payload_preset=WebhookPreset.custom,
    )
    assert "super-secret-token" not in repr(sr)  # invariant 3: never in a repr
    assert sr.secret == "super-secret-token"  # still reachable by attribute


def test_folder_route_fields_frozen_slotted() -> None:
    fr = FolderRoute(
        server_id=1,
        server_name="plex-main",
        path="/data/media/tv",
        extensions=frozenset({"mkv", "srt"}),
        library_id="2",
        scan_mode=ScanMode.targeted,
        event_types=frozenset(FsEventType),
    )
    assert fr.path == "/data/media/tv"
    assert fr.extensions == frozenset({"mkv", "srt"})
    assert fr.event_types == frozenset(FsEventType)
    assert not hasattr(fr, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fr.path = "/elsewhere"  # type: ignore[misc]


def test_runtime_config_fields_frozen_slotted() -> None:
    sr = ServerRuntime(
        server_id=1,
        name="plex-main",
        type=ServerType.plex,
        base_url="",
        verify_tls=True,
        timeout_seconds=10.0,
        secret=None,
        scan_mode=ScanMode.targeted,
        debounce_mode=DebounceMode.trailing,
        debounce_window_seconds=30,
        retry_attempts=3,
        webhook_method=None,
        webhook_headers_json=None,
        webhook_body_template=None,
        webhook_payload_preset=WebhookPreset.custom,
    )
    fr = FolderRoute(
        server_id=1,
        server_name="plex-main",
        path="/data/media/tv",
        extensions=frozenset(),
        library_id="2",
        scan_mode=ScanMode.targeted,
        event_types=frozenset(FsEventType),
    )
    cfg = RuntimeConfig(
        watch_paths=frozenset({"/data/media/tv"}),
        routes=(fr,),
        servers={1: sr},
        ignore_dirs=frozenset({"@eaDir"}),
    )
    assert cfg.watch_paths == frozenset({"/data/media/tv"})
    assert cfg.routes == (fr,)
    assert cfg.servers == {1: sr}
    assert cfg.ignore_dirs == frozenset({"@eaDir"})
    assert not hasattr(cfg, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.routes = ()  # type: ignore[misc]


@dataclass
class FakeRepo:
    """Typed structural stub for db.repo.Repo, exposing ONLY the methods that
    build_runtime_config calls. Returns transient (never session-added) section-2
    model instances; resolve_secret returns the already-"decrypted" plaintext."""

    servers: list[Server] = field(default_factory=list)
    folders_by_server: dict[int, list[Folder]] = field(default_factory=dict)
    secrets: dict[int, str | None] = field(default_factory=dict)

    def list_servers(self, *, enabled_only: bool = False) -> list[Server]:
        if enabled_only:
            return [s for s in self.servers if s.enabled]
        return list(self.servers)

    def list_folders(self, server_id: int) -> list[Folder]:
        return list(self.folders_by_server.get(server_id, []))

    def resolve_secret(self, server: Server) -> str | None:
        if server.id is None:
            return None
        return self.secrets.get(server.id)


def make_server(
    server_id: int,
    *,
    name: str,
    type: ServerType = ServerType.plex,
    base_url: str = "https://plex.local:32400",
    scan_mode: ScanMode = ScanMode.targeted,
    debounce_mode: DebounceMode = DebounceMode.trailing,
    enabled: bool = True,
    webhook_payload_preset: WebhookPreset = WebhookPreset.custom,
    event_types: str = "created,moved_to,deleted,moved_from",
) -> Server:
    return Server(
        id=server_id,
        name=name,
        type=type,
        base_url=base_url,
        verify_tls=True,
        timeout_seconds=10.0,
        secret_encrypted="ciphertext-ignored-by-stub",
        scan_mode=scan_mode,
        debounce_mode=debounce_mode,
        debounce_window_seconds=30,
        retry_attempts=3,
        enabled=enabled,
        webhook_payload_preset=webhook_payload_preset,
        event_types=event_types,
    )


def make_folder(
    folder_id: int,
    *,
    server_id: int,
    path: str,
    library_id: str | None,
    extensions: list[str],
    enabled: bool = True,
) -> Folder:
    folder = Folder(
        id=folder_id,
        server_id=server_id,
        path=path,
        library_id=library_id,
        enabled=enabled,
    )
    folder.filetypes = [FileType(id=None, folder_id=folder_id, extension=ext) for ext in extensions]
    return folder


def test_build_runtime_config_happy_path() -> None:
    server = make_server(1, name="plex-main")
    folder = make_folder(
        10, server_id=1, path="/data/media/tv/", library_id="2", extensions=["MKV", ".srt"]
    )
    repo = FakeRepo(
        servers=[server],
        folders_by_server={1: [folder]},
        secrets={1: "plex-token-xyz"},
    )

    cfg = build_runtime_config(cast("Repo", repo))

    # One server, decrypted secret surfaced into ServerRuntime.
    assert set(cfg.servers) == {1}
    sr = cfg.servers[1]
    assert sr.server_id == 1
    assert sr.name == "plex-main"
    assert sr.type is ServerType.plex
    assert sr.secret == "plex-token-xyz"
    assert sr.scan_mode is ScanMode.targeted
    assert sr.debounce_mode is DebounceMode.trailing
    assert sr.debounce_window_seconds == 30
    assert sr.retry_attempts == 3

    # One route, normalized path (trailing slash stripped) + normalized extensions.
    assert len(cfg.routes) == 1
    route = cfg.routes[0]
    assert route.server_id == 1
    assert route.server_name == "plex-main"
    assert route.path == "/data/media/tv"
    assert route.extensions == frozenset({"mkv", "srt"})
    assert route.library_id == "2"
    assert route.scan_mode is ScanMode.targeted
    assert route.event_types == frozenset(FsEventType)

    # Watch set is the normalized path; ignore dirs come from defaults.
    assert cfg.watch_paths == frozenset({"/data/media/tv"})
    assert "@eaDir" in cfg.ignore_dirs


def test_disabled_server_excluded() -> None:
    enabled = make_server(1, name="plex-on", enabled=True)
    disabled = make_server(2, name="plex-off", enabled=False)
    repo = FakeRepo(
        servers=[enabled, disabled],
        folders_by_server={
            1: [make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=["mkv"])],
            2: [make_folder(20, server_id=2, path="/data/off", library_id="9", extensions=["mkv"])],
        },
        secrets={1: "tok1", 2: "tok2"},
    )

    cfg = build_runtime_config(cast("Repo", repo))

    assert set(cfg.servers) == {1}
    assert all(r.server_id == 1 for r in cfg.routes)
    assert cfg.watch_paths == frozenset({"/data/tv"})
    assert "/data/off" not in cfg.watch_paths


def test_disabled_folder_excluded() -> None:
    server = make_server(1, name="plex-main")
    on = make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=["mkv"])
    off = make_folder(
        11, server_id=1, path="/data/hidden", library_id="3", extensions=["mkv"], enabled=False
    )
    repo = FakeRepo(servers=[server], folders_by_server={1: [on, off]}, secrets={1: "tok"})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.watch_paths == frozenset({"/data/tv"})
    assert [r.path for r in cfg.routes] == ["/data/tv"]


def test_watch_paths_dedup_two_folders_same_path() -> None:
    # Two servers watch the SAME host path -> one watch path, two routes.
    s1 = make_server(1, name="plex")
    s2 = make_server(2, name="emby", type=ServerType.emby, scan_mode=ScanMode.library)
    repo = FakeRepo(
        servers=[s1, s2],
        folders_by_server={
            1: [make_folder(10, server_id=1, path="/data/tv/", library_id="2", extensions=["mkv"])],
            2: [make_folder(20, server_id=2, path="/data/tv", library_id="5", extensions=["mkv"])],
        },
        secrets={1: "a", 2: "b"},
    )

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.watch_paths == frozenset({"/data/tv"})
    assert len(cfg.routes) == 2
    assert {r.server_id for r in cfg.routes} == {1, 2}


def test_empty_filetypes_means_all_extensions() -> None:
    server = make_server(1, name="plex-main")
    folder = make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=[])
    repo = FakeRepo(servers=[server], folders_by_server={1: [folder]}, secrets={1: "tok"})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.routes[0].extensions == frozenset()


def test_secret_none_when_unresolved() -> None:
    server = make_server(1, name="plex-main")
    folder = make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=["mkv"])
    repo = FakeRepo(servers=[server], folders_by_server={1: [folder]}, secrets={})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.servers[1].secret is None


def test_empty_repo_yields_empty_config() -> None:
    cfg = build_runtime_config(cast("Repo", FakeRepo()))
    assert cfg.servers == {}
    assert cfg.routes == ()
    assert cfg.watch_paths == frozenset()
    assert "@eaDir" in cfg.ignore_dirs


def test_build_runtime_config_carries_webhook_payload_preset() -> None:
    server = make_server(
        1,
        name="hook",
        type=ServerType.webhook,
        webhook_payload_preset=WebhookPreset.sonarr_radarr,
    )
    repo = FakeRepo(servers=[server], folders_by_server={}, secrets={1: None})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.servers[1].webhook_payload_preset == WebhookPreset.sonarr_radarr


def test_build_runtime_config_carries_narrowed_event_types() -> None:
    server = make_server(1, name="hook", event_types="created,deleted")
    folder = make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=["mkv"])
    repo = FakeRepo(servers=[server], folders_by_server={1: [folder]}, secrets={1: None})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.routes[0].event_types == frozenset({FsEventType.created, FsEventType.deleted})
