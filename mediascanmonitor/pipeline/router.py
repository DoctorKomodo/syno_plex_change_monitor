from mediascanmonitor.config.runtime import RuntimeConfig
from mediascanmonitor.db.models import ScanMode
from mediascanmonitor.pipeline.events import FsEvent, ScanRequest
from mediascanmonitor.pipeline.filters import extension_matches, is_ignored


def compute_scan_path(folder_root: str, file_path: str) -> tuple[str, str | None]:
    """Return ``(scan_path, top_folder)`` for a file under ``folder_root``.

    ``scan_path`` is ``folder_root`` joined with the first path segment of ``file_path`` below
    it (the proven Plex show/movie-folder behavior). If the file sits directly in
    ``folder_root`` (no intermediate folder), ``top_folder`` is ``None`` and ``scan_path`` ==
    ``folder_root``. Callers guarantee ``file_path`` is below ``folder_root``.
    """
    relative = file_path[len(folder_root) :].lstrip("/")
    parts = relative.split("/")
    if len(parts) >= 2:
        top_folder = parts[0]
        return f"{folder_root.rstrip('/')}/{top_folder}", top_folder
    return folder_root, None


def _is_path_prefix(prefix: str, path: str) -> bool:
    """Segment-aware prefix test: ``/a/b`` matches ``/a/b`` and ``/a/b/c`` but not ``/a/bc``."""
    if path == prefix:
        return True
    prefix_with_sep = prefix if prefix.endswith("/") else f"{prefix}/"
    return path.startswith(prefix_with_sep)


def route(event: FsEvent, config: RuntimeConfig) -> list[ScanRequest]:
    """Map a filesystem event to one ``ScanRequest`` per matching ``(server, folder)`` route.

    A route matches when its ``path`` is a segment-prefix of ``event.path``, the event path is
    not inside an ignored directory, the file extension matches the route's extension set (empty
    set => all), and the route's server is subscribed to the event's type (empty set => none).
    ``scan_path``/``top_folder``/``scan_key`` are computed per the route's ``scan_mode``
    (invariant 2).
    """
    if is_ignored(event.path, config.ignore_dirs):
        return []

    requests: list[ScanRequest] = []
    for folder_route in config.routes:
        if not _is_path_prefix(folder_route.path, event.path):
            continue
        if not extension_matches(event.path, folder_route.extensions):
            continue
        if event.event_type not in folder_route.event_types:
            continue

        if folder_route.scan_mode is ScanMode.targeted:
            scan_path, top_folder = compute_scan_path(folder_route.path, event.path)
            scan_key = scan_path
        else:
            scan_path = None
            top_folder = None
            scan_key = f"lib:{folder_route.library_id}"

        requests.append(
            ScanRequest(
                server_id=folder_route.server_id,
                server_name=folder_route.server_name,
                scan_mode=folder_route.scan_mode,
                scan_path=scan_path,
                library_id=folder_route.library_id,
                scan_key=scan_key,
                event_type=event.event_type,
                file_path=event.path,
                top_folder=top_folder,
            )
        )
    return requests
