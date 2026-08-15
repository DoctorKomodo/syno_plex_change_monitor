"""Pydantic boundary models for repo writes (contract section 4).

`secret` is **plaintext-in**: the caller supplies the raw token and the repo encrypts it
before storage. `ServerUpdate` is a partial-update model — callers send only the fields
they want changed, and the repo applies them via ``model_dump(exclude_unset=True)``.
"""

import os

from pydantic import BaseModel, Field, field_validator

from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
)
from mediascanmonitor.normalize import normalize_extension, normalize_path


class ServerCreate(BaseModel):
    name: str
    type: ServerType
    base_url: str = ""
    verify_tls: bool = True
    timeout_seconds: float = 10.0
    # plaintext, encrypted by the repo; repr=False keeps it out of __repr__/logs/tracebacks
    # and Pydantic validation errors (contract invariant 3).
    secret: str | None = Field(default=None, repr=False)
    scan_mode: ScanMode = ScanMode.targeted
    debounce_mode: DebounceMode = DebounceMode.trailing
    debounce_window_seconds: int = 30
    retry_attempts: int = 3
    enabled: bool = True
    webhook_method: str | None = None
    webhook_headers_json: str | None = None
    webhook_body_template: str | None = None
    webhook_payload_preset: WebhookPreset = WebhookPreset.custom
    event_types: list[FsEventType] = Field(default_factory=lambda: list(FsEventType))


class ServerUpdate(BaseModel):
    name: str | None = None
    type: ServerType | None = None
    base_url: str | None = None
    verify_tls: bool | None = None
    timeout_seconds: float | None = None
    # plaintext secret tri-state via exclude_unset: a str re-encrypts; explicit None clears
    # the stored secret; omitting the field entirely leaves secret_encrypted unchanged.
    # repr=False keeps plaintext out of __repr__/logs/tracebacks (contract invariant 3).
    secret: str | None = Field(default=None, repr=False)
    scan_mode: ScanMode | None = None
    debounce_mode: DebounceMode | None = None
    debounce_window_seconds: int | None = None
    retry_attempts: int | None = None
    enabled: bool | None = None
    webhook_method: str | None = None
    webhook_headers_json: str | None = None
    webhook_body_template: str | None = None
    webhook_payload_preset: WebhookPreset | None = None
    event_types: list[FsEventType] | None = None


class FolderCreate(BaseModel):
    path: str
    library_id: str | None = None
    library_name: str | None = None
    extensions: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("path")
    @classmethod
    def _normalize_and_require_absolute(cls, value: str) -> str:
        normalized = normalize_path(value)
        if not os.path.isabs(normalized):
            raise ValueError(f"folder path must be absolute, got {value!r}")
        return normalized

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        # normalize, drop empties, dedupe (order-preserving) so the DB never gets
        # duplicate FileType rows. Empty result == "match all" (cross-plan invariant 1).
        out: list[str] = []
        for ext in value:
            norm = normalize_extension(ext)
            if norm and norm not in out:
                out.append(norm)
        return out


class FolderUpdate(BaseModel):
    path: str | None = None
    library_id: str | None = None
    extensions: list[str] | None = None
    enabled: bool | None = None

    @field_validator("path")
    @classmethod
    def _normalize_and_require_absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_path(value)
        if not os.path.isabs(normalized):
            raise ValueError(f"folder path must be absolute, got {value!r}")
        return normalized

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str] | None) -> list[str] | None:
        # Same rule as FolderCreate: normalize, drop empties, dedupe (order-preserving).
        # None == "field omitted / leave unchanged"; [] == "clear all filetypes".
        if value is None:
            return None
        out: list[str] = []
        for ext in value:
            norm = normalize_extension(ext)
            if norm and norm not in out:
                out.append(norm)
        return out
