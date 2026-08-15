"""Immutable runtime configuration snapshot, assembled from the DB.

The router and dispatcher (sub-plans 05/06) read this snapshot. Secrets are decrypted
into ``ServerRuntime.secret`` here (in memory only) — adapters receive plaintext tokens.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mediascanmonitor.config.defaults import IGNORE_DIRS
from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
    parse_event_types,
)
from mediascanmonitor.normalize import normalize_extension, normalize_path

if TYPE_CHECKING:
    from mediascanmonitor.db.repo import Repo


@dataclass(frozen=True, slots=True)
class ServerRuntime:
    server_id: int
    name: str
    type: ServerType
    base_url: str
    verify_tls: bool
    timeout_seconds: float
    secret: str | None = field(repr=False)  # decrypted plaintext; excluded from repr (invariant 3)
    scan_mode: ScanMode
    debounce_mode: DebounceMode
    debounce_window_seconds: int
    retry_attempts: int
    webhook_method: str | None
    webhook_headers_json: str | None
    webhook_body_template: str | None
    webhook_payload_preset: WebhookPreset


@dataclass(frozen=True, slots=True)
class FolderRoute:
    server_id: int
    server_name: str
    path: str  # watched folder root (normalized, no trailing slash)
    extensions: frozenset[str]  # normalized; EMPTY SET MEANS "match all extensions"
    library_id: str | None
    scan_mode: ScanMode
    event_types: frozenset[FsEventType]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    watch_paths: frozenset[str]  # dedup union of enabled folder paths
    routes: tuple[FolderRoute, ...]  # one per enabled (server, folder)
    servers: dict[int, ServerRuntime]  # by server_id (enabled only)
    ignore_dirs: frozenset[str]


def build_runtime_config(repo: Repo) -> RuntimeConfig:
    """Read enabled servers/folders/filetypes from the DB, decrypt secrets, and assemble the
    immutable snapshot. Disabled servers and their folders are excluded."""
    servers: dict[int, ServerRuntime] = {}
    routes: list[FolderRoute] = []
    watch_paths: set[str] = set()

    for server in repo.list_servers(enabled_only=True):
        assert server.id is not None  # persisted servers always carry an id
        server_id = server.id
        servers[server_id] = ServerRuntime(
            server_id=server_id,
            name=server.name,
            type=server.type,
            base_url=server.base_url,
            verify_tls=server.verify_tls,
            timeout_seconds=server.timeout_seconds,
            secret=repo.resolve_secret(server),
            scan_mode=server.scan_mode,
            debounce_mode=server.debounce_mode,
            debounce_window_seconds=server.debounce_window_seconds,
            retry_attempts=server.retry_attempts,
            webhook_method=server.webhook_method,
            webhook_headers_json=server.webhook_headers_json,
            webhook_body_template=server.webhook_body_template,
            webhook_payload_preset=server.webhook_payload_preset,
        )
        for folder in repo.list_folders(server_id):
            if not folder.enabled:
                continue
            path = normalize_path(folder.path)
            watch_paths.add(path)
            routes.append(
                FolderRoute(
                    server_id=server_id,
                    server_name=server.name,
                    path=path,
                    extensions=frozenset(
                        normalize_extension(ft.extension) for ft in folder.filetypes
                    ),
                    library_id=folder.library_id,
                    scan_mode=server.scan_mode,
                    event_types=parse_event_types(server.event_types),
                )
            )

    return RuntimeConfig(
        watch_paths=frozenset(watch_paths),
        routes=tuple(routes),
        servers=servers,
        ignore_dirs=IGNORE_DIRS,
    )
