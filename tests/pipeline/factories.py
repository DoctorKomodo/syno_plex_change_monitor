from collections.abc import Iterable

from mediascanmonitor.config.runtime import FolderRoute, RuntimeConfig, ServerRuntime
from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
)


def make_server_runtime(
    *,
    server_id: int = 1,
    name: str = "plex-1",
    server_type: ServerType = ServerType.plex,
    scan_mode: ScanMode = ScanMode.targeted,
    debounce_mode: DebounceMode = DebounceMode.trailing,
    debounce_window_seconds: int = 30,
) -> ServerRuntime:
    return ServerRuntime(
        server_id=server_id,
        name=name,
        type=server_type,
        base_url="http://plex:32400",
        verify_tls=True,
        timeout_seconds=10.0,
        secret="token",
        scan_mode=scan_mode,
        debounce_mode=debounce_mode,
        debounce_window_seconds=debounce_window_seconds,
        retry_attempts=3,
        webhook_method=None,
        webhook_headers_json=None,
        webhook_body_template=None,
        webhook_payload_preset=WebhookPreset.custom,
    )


def make_folder_route(
    *,
    server_id: int = 1,
    server_name: str = "plex-1",
    path: str = "/data/tv",
    extensions: frozenset[str] = frozenset({"mkv"}),
    library_id: str | None = "2",
    scan_mode: ScanMode = ScanMode.targeted,
    event_types: frozenset[FsEventType] = frozenset(FsEventType),
) -> FolderRoute:
    return FolderRoute(
        server_id=server_id,
        server_name=server_name,
        path=path,
        extensions=extensions,
        library_id=library_id,
        scan_mode=scan_mode,
        event_types=event_types,
    )


def make_runtime_config(
    routes: Iterable[FolderRoute],
    *,
    servers: dict[int, ServerRuntime] | None = None,
    ignore_dirs: frozenset[str] = frozenset({"@eaDir", "#snapshot"}),
) -> RuntimeConfig:
    routes_tuple = tuple(routes)
    return RuntimeConfig(
        watch_paths=frozenset(r.path for r in routes_tuple),
        routes=routes_tuple,
        servers=servers if servers is not None else {},
        ignore_dirs=ignore_dirs,
    )
