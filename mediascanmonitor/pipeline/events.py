"""Domain event and scan-request types for the watcher → pipeline boundary.

Frozen, slotted dataclasses — these flow from the watcher (sub-plan 04) through the
router/debouncer/dispatcher (sub-plan 05) and must be cheap, immutable, and hashable
where needed.
"""

from dataclasses import dataclass

from mediascanmonitor.db.models import FsEventType, ScanMode

__all__ = ["FsEventType", "FsEvent", "ScanRequest"]


@dataclass(frozen=True, slots=True)
class FsEvent:
    path: str  # absolute path of the changed entry
    event_type: FsEventType
    is_dir: bool


@dataclass(frozen=True, slots=True)
class ScanRequest:
    server_id: int
    server_name: str
    scan_mode: ScanMode
    scan_path: str | None  # host path to scan (targeted); None for library mode
    library_id: str | None  # backend library/section id
    scan_key: str  # debounce key: scan_path (targeted) or f"lib:{library_id}"
    # context (used by webhook templating in Phase 2; carried now):
    event_type: FsEventType
    file_path: str  # the originating absolute file path
    top_folder: str | None  # first path segment under the folder root (targeted), else None
