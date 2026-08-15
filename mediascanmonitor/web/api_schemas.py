"""Redacted API read-models + per-server-type field specs (contract §D).

INVARIANT (CLAUDE.md rule 5 / contract invariant 1): no read-model here ever carries the
secret or its ciphertext — the only secret signal is ``has_secret: bool``. Writes reuse the
plaintext-in write-schemas (ServerCreate/ServerUpdate/FolderCreate/FolderUpdate); reads redact.

SERVER_TYPE_SPECS is the ONE place per-type rules live (rule 2): routers/templates read it +
``registry.get_adapter_class(type).supported_scan_modes`` and never branch on a literal type name.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from mediascanmonitor.db.models import (
    DebounceMode,
    Folder,
    FsEventType,
    ScanMode,
    Server,
    ServerType,
    WebhookPreset,
    parse_event_types,
)
from mediascanmonitor.observ.events_bus import EventRecord
from mediascanmonitor.servers.registry import get_adapter_class


class FolderRead(BaseModel):
    id: int
    server_id: int
    path: str
    library_id: str | None
    library_name: str | None
    enabled: bool
    extensions: list[str]  # sorted normalized extensions

    @classmethod
    def from_model(cls, folder: Folder) -> FolderRead:
        assert folder.id is not None
        return cls(
            id=folder.id,
            server_id=folder.server_id,
            path=folder.path,
            library_id=folder.library_id,
            library_name=folder.library_name,
            enabled=folder.enabled,
            extensions=sorted(ft.extension for ft in folder.filetypes),
        )


class ServerRead(BaseModel):
    id: int
    name: str
    type: ServerType
    base_url: str
    verify_tls: bool
    timeout_seconds: float
    has_secret: bool  # server.secret_encrypted is not None — NEVER the token/ciphertext
    scan_mode: ScanMode
    debounce_mode: DebounceMode
    debounce_window_seconds: int
    retry_attempts: int
    enabled: bool
    supported_scan_modes: list[ScanMode]
    webhook_method: str | None
    webhook_headers_json: str | None
    webhook_body_template: str | None
    webhook_payload_preset: WebhookPreset
    event_types: list[FsEventType]
    folders: list[FolderRead]

    @classmethod
    def from_model(cls, server: Server, folders: list[Folder]) -> ServerRead:
        assert server.id is not None
        return cls(
            id=server.id,
            name=server.name,
            type=server.type,
            base_url=server.base_url,
            verify_tls=server.verify_tls,
            timeout_seconds=server.timeout_seconds,
            has_secret=server.secret_encrypted is not None,
            scan_mode=server.scan_mode,
            debounce_mode=server.debounce_mode,
            debounce_window_seconds=server.debounce_window_seconds,
            retry_attempts=server.retry_attempts,
            enabled=server.enabled,
            supported_scan_modes=sorted(get_adapter_class(server.type).supported_scan_modes),
            webhook_method=server.webhook_method,
            webhook_headers_json=server.webhook_headers_json,
            webhook_body_template=server.webhook_body_template,
            webhook_payload_preset=server.webhook_payload_preset,
            event_types=[t for t in FsEventType if t in parse_event_types(server.event_types)],
            folders=[FolderRead.from_model(f) for f in folders],
        )


class ServerTestResponse(BaseModel):
    ok: bool
    detail: str


class EventRead(BaseModel):
    ts: str
    server_id: int
    server_name: str
    scan_mode: str
    scan_key: str
    scan_path: str | None
    library_id: str | None
    event_type: str
    file_path: str
    ok: bool
    status_code: int | None
    detail: str

    @classmethod
    def from_record(cls, record: EventRecord) -> EventRead:
        return cls(
            ts=record.ts,
            server_id=record.server_id,
            server_name=record.server_name,
            scan_mode=record.scan_mode,
            scan_key=record.scan_key,
            scan_path=record.scan_path,
            library_id=record.library_id,
            event_type=record.event_type,
            file_path=record.file_path,
            ok=record.ok,
            status_code=record.status_code,
            detail=record.detail,
        )


@dataclass(frozen=True, slots=True)
class ServerTypeSpec:
    requires_secret: bool  # a token is mandatory at save time
    requires_base_url: bool  # base_url must be non-empty at save time
    is_webhook: bool  # exposes the webhook_* template fields
    supports_secret: bool = True  # whether the token field is shown at all
    # Label/placeholder for the base_url field. Media servers take a host:port base the
    # adapter appends API paths to; a webhook takes the full endpoint URL it POSTs to.
    base_url_label: str = "Base URL"
    base_url_placeholder: str = "http://host:port"
    # Hint shown under the token field — what the secret is for on this backend.
    secret_hint: str = "Access token / API key for this server."
    # Hint shown beside the Test button — what a connection test actually does here.
    test_hint: str = "Checks the URL and token work."


SERVER_TYPE_SPECS: dict[ServerType, ServerTypeSpec] = {
    ServerType.plex: ServerTypeSpec(requires_secret=True, requires_base_url=True, is_webhook=False),
    ServerType.emby: ServerTypeSpec(requires_secret=True, requires_base_url=True, is_webhook=False),
    ServerType.jellyfin: ServerTypeSpec(
        requires_secret=True, requires_base_url=True, is_webhook=False
    ),
    ServerType.audiobookshelf: ServerTypeSpec(
        requires_secret=True, requires_base_url=True, is_webhook=False
    ),
    ServerType.webhook: ServerTypeSpec(
        requires_secret=False,
        requires_base_url=True,
        is_webhook=True,
        base_url_label="Endpoint URL",
        base_url_placeholder="https://host/path",
        secret_hint=(
            "Optional, stored encrypted. Reference it in a header or the body template as "
            "{{ secret }} — e.g. Authorization: Bearer {{ secret }}."
        ),
        test_hint="Sends a test event to the endpoint.",
    ),
}
