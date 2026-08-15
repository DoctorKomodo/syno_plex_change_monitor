"""SQLModel persistence models and enums (frozen interface contract, sections 1-2).

`FileType` is its own table so the `Server >- Folder >- FileType` cascade delete can be
tested explicitly. Secrets live only as Fernet ciphertext in `Server.secret_encrypted`;
plaintext never touches a model field.

Forward references (``list[Folder]``, ``list[FileType]``) are left UNQUOTED and this module —
like the rest of the package — uses no ``from __future__ import annotations``: on Python 3.14
PEP 649 defers annotation evaluation, so SQLModel/SQLAlchemy resolve relationship targets to the
real classes when the mappers configure. The PEP 563 future import would instead stringize the
annotation to ``"list['Folder']"`` and break mapper configuration.
"""

from collections.abc import Iterable
from enum import StrEnum

from sqlmodel import Field, Relationship, SQLModel


class ServerType(StrEnum):
    webhook = "webhook"
    plex = "plex"
    emby = "emby"
    jellyfin = "jellyfin"
    audiobookshelf = "audiobookshelf"


class ScanMode(StrEnum):
    targeted = "targeted"  # backend scans a specific folder path (Plex ?path=)
    library = "library"  # backend refreshes a whole library id


class DebounceMode(StrEnum):
    off = "off"  # dispatch every matching event
    trailing = "trailing"  # collapse a burst per (server_id, scan_key) after a window


class WebhookPreset(StrEnum):
    custom = "custom"  # render webhook_body_template (today's behaviour)
    sonarr_radarr = "sonarr_radarr"  # subtitle-pruner-compatible payload


class FsEventType(StrEnum):
    created = "created"  # inotify CREATE
    moved_to = "moved_to"  # inotify MOVED_TO
    deleted = "deleted"  # inotify DELETE
    moved_from = "moved_from"  # inotify MOVED_FROM


def serialize_event_types(types: Iterable[FsEventType]) -> str:
    """Join event types into the comma-separated form ``Server.event_types`` stores."""
    return ",".join(t.value for t in types)


def parse_event_types(raw: str) -> frozenset[FsEventType]:
    """Inverse of ``serialize_event_types``. An empty string parses to an empty set."""
    return frozenset(FsEventType(v) for v in raw.split(",") if v)


class Server(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    type: ServerType
    base_url: str = ""  # target URL; for a webhook this is the full endpoint URL
    verify_tls: bool = True
    timeout_seconds: float = 10.0
    secret_encrypted: str | None = None  # Fernet token; never the plaintext
    scan_mode: ScanMode = ScanMode.targeted
    debounce_mode: DebounceMode = DebounceMode.trailing
    debounce_window_seconds: int = 30
    retry_attempts: int = 3  # total tries (1 = no retry)
    enabled: bool = True
    # webhook-only (unused until Phase 2, defined now to avoid a Phase 2 migration):
    webhook_method: str | None = None
    webhook_headers_json: str | None = None
    webhook_body_template: str | None = None
    webhook_payload_preset: WebhookPreset = WebhookPreset.custom
    folders: list[Folder] = Relationship(
        back_populates="server",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Folder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(foreign_key="server.id", ondelete="CASCADE", index=True)
    path: str  # host path watched, e.g. /data/media/tvseries
    library_id: str | None = None  # backend section/library id; None for webhook
    library_name: str | None = None  # human label for library_id; display-only, set via the picker
    enabled: bool = True
    server: Server = Relationship(back_populates="folders")
    filetypes: list[FileType] = Relationship(
        back_populates="folder",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class FileType(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    folder_id: int = Field(foreign_key="folder.id", ondelete="CASCADE", index=True)
    extension: str  # normalized: lowercase, no leading dot
    folder: Folder = Relationship(back_populates="filetypes")


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)  # e.g. "password_hash", "inotify_gate"
    value: str
