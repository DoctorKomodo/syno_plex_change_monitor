"""Browser UI: server-rendered pages, htmx /ui form handlers, and the SSE event stream.

All routes are guarded by ``require_page_auth`` at router level (contract §B): unauthenticated
requests get a 303 redirect to /login (or /setup when no password is set). The only
unauthenticated web surface — /login, /setup, /static/* — is owned elsewhere (01 / StaticFiles).

The /ui/* mutations are thin presentations of the SAME write as /api/*: they parse
``Form(...)``, build the existing write-schemas, and call the shared write-cores in
``web/writes.py`` (contract §J), so they validate (incl. the §D token-required 422), write
off-thread, and ``rebuild_engine`` identically to the JSON API. They differ only in input
parsing and HTML-partial output (invariant 4).

SSE (contract §K): a plain ``StreamingResponse(media_type="text/event-stream")`` over an async
generator that replays ``bus.recent()`` then yields ``bus.subscribe()`` frames as
``data: {json}\\n\\n``, breaking on ``await request.is_disconnected()``. No sse-starlette.
Known, accepted race: a record published between the recent() snapshot and subscribe()
registration may be missed or duplicated.
"""

import asyncio
import dataclasses
import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData
from starlette.responses import Response

from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
)
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.db.schemas import FolderCreate, ServerCreate, ServerUpdate
from mediascanmonitor.engine import Engine
from mediascanmonitor.observ.events_bus import EventRecord, EventsBus
from mediascanmonitor.servers import registry
from mediascanmonitor.servers.base import LibraryListResult
from mediascanmonitor.servers.webhook import HTTP_METHODS
from mediascanmonitor.servers.webhook_presets import WEBHOOK_PRESETS
from mediascanmonitor.web.api_schemas import SERVER_TYPE_SPECS, ServerRead, ServerTestResponse
from mediascanmonitor.web.deps import (
    get_engine,
    get_events_bus,
    get_repo,
    get_templates,
    require_page_auth,
)
from mediascanmonitor.web.fsbrowse import DirListing, list_directory
from mediascanmonitor.web.rebuild import rebuild_engine
from mediascanmonitor.web.serverprobe import (
    run_connectivity_test,
    run_library_listing,
    runtime_from_create,
    runtime_from_server,
)
from mediascanmonitor.web.writes import (
    apply_server_create_with_folders,
    apply_server_delete,
    apply_server_update_with_folders,
)

router = APIRouter(dependencies=[Depends(require_page_auth)])


def _sse_frame(record: EventRecord) -> str:
    """Serialize a (secret-free) EventRecord as one SSE ``data:`` frame."""
    return f"data: {json.dumps(dataclasses.asdict(record))}\n\n"


async def _event_generator(request: Request, bus: EventsBus) -> AsyncIterator[str]:
    for record in bus.recent():
        yield _sse_frame(record)
    async for record in bus.subscribe():
        if await request.is_disconnected():
            break
        yield _sse_frame(record)


@router.get("/events/stream")
async def events_stream(
    request: Request, bus: EventsBus = Depends(get_events_bus)
) -> StreamingResponse:
    return StreamingResponse(
        _event_generator(request, bus),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _scan_modes_by_type() -> dict[str, list[str]]:
    """Return {type value: sorted scan-mode values}.

    Drives the add-server form without literal type-name branching (invariant 6).
    """
    return {
        server_type.value: sorted(
            mode.value for mode in registry.get_adapter_class(server_type).supported_scan_modes
        )
        for server_type in ServerType
    }


_EVENT_TYPE_LABELS: dict[FsEventType, str] = {
    FsEventType.created: "Created",
    FsEventType.moved_to: "Moved in",
    FsEventType.deleted: "Deleted",
    FsEventType.moved_from: "Moved out",
}


def _webhook_preset_options() -> list[tuple[str, str]]:
    """(value, label) for the webhook payload-preset <select>: Custom first, then the registry."""
    options: list[tuple[str, str]] = [(WebhookPreset.custom.value, "Custom")]
    options += [(preset.value, definition.label) for preset, definition in WEBHOOK_PRESETS.items()]
    return options


def _type_specs() -> dict[str, dict[str, bool | str]]:
    """Serialize SERVER_TYPE_SPECS for the template/JS (the one place per-type rules live, §D)."""
    return {
        server_type.value: {
            "requires_secret": spec.requires_secret,
            "requires_base_url": spec.requires_base_url,
            "is_webhook": spec.is_webhook,
            "supports_secret": spec.supports_secret,
            "base_url_label": spec.base_url_label,
            "base_url_placeholder": spec.base_url_placeholder,
            "secret_hint": spec.secret_hint,
            "test_hint": spec.test_hint,
            "supports_library_discovery": registry.get_adapter_class(
                server_type
            ).supports_library_discovery,
        }
        for server_type, spec in SERVER_TYPE_SPECS.items()
    }


async def _status_context(repo: Repo, engine: Engine) -> dict[str, Any]:
    """Shared dashboard/settings status context — same primitives as /api/status (§H)."""
    gate = await asyncio.to_thread(repo.get_setting, "inotify_gate")
    servers = await asyncio.to_thread(repo.list_servers)
    limit = engine.watch_limit
    return {
        "engine_state": engine.state.value,
        "inotify_gate": gate or "enforce",
        "watch": limit,  # WatchLimitStatus | None (.current/.dirs/.needed/.recommended/.ok)
        "server_count": len(servers),
        "enabled_server_count": sum(1 for s in servers if s.enabled),
    }


@router.get("/")
async def dashboard(
    request: Request,
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    context = await _status_context(repo, engine)
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


@router.get("/servers")
async def servers_page(
    request: Request,
    repo: Repo = Depends(get_repo),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    servers = await asyncio.to_thread(repo.list_servers)
    return templates.TemplateResponse(
        request=request,
        name="servers.html",
        context={"servers": servers},
    )


@router.get("/servers/new")
async def server_new_page(
    request: Request, templates: Jinja2Templates = Depends(get_templates)
) -> Response:
    # Declared before /servers/{server_id} so the literal path wins over the int converter.
    return templates.TemplateResponse(
        request=request,
        name="server_new.html",
        context={
            "creating": True,
            "server": None,
            "server_types": [t.value for t in ServerType],
            "scan_modes": [m.value for m in ScanMode],
            "debounce_modes": [m.value for m in DebounceMode],
            "event_type_options": list(_EVENT_TYPE_LABELS.items()),
            "type_specs": _type_specs(),
            "scan_modes_by_type": _scan_modes_by_type(),
            "webhook_presets": _webhook_preset_options(),
            "webhook_methods": list(HTTP_METHODS),
            # Initial per-type strings for the first type; JS re-applies them on type change.
            "base_url_label": SERVER_TYPE_SPECS[next(iter(ServerType))].base_url_label,
            "base_url_placeholder": SERVER_TYPE_SPECS[next(iter(ServerType))].base_url_placeholder,
            "secret_hint": SERVER_TYPE_SPECS[next(iter(ServerType))].secret_hint,
            "test_hint": SERVER_TYPE_SPECS[next(iter(ServerType))].test_hint,
        },
    )


async def _load_server_read(repo: Repo, server_id: int) -> ServerRead:
    server = await asyncio.to_thread(repo.get_server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail=f"server {server_id} not found")
    folders = await asyncio.to_thread(repo.list_folders, server_id)
    return ServerRead.from_model(server, folders)


@router.get("/servers/{server_id}")
async def server_detail(
    request: Request,
    server_id: int,
    repo: Repo = Depends(get_repo),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    server = await _load_server_read(repo, server_id)
    return templates.TemplateResponse(
        request=request,
        name="server_detail.html",
        context={
            "creating": False,
            "server": server,
            "debounce_modes": [m.value for m in DebounceMode],
            "event_type_options": list(_EVENT_TYPE_LABELS.items()),
            "is_webhook": SERVER_TYPE_SPECS[server.type].is_webhook,
            "base_url_label": SERVER_TYPE_SPECS[server.type].base_url_label,
            "base_url_placeholder": SERVER_TYPE_SPECS[server.type].base_url_placeholder,
            "secret_hint": SERVER_TYPE_SPECS[server.type].secret_hint,
            "test_hint": SERVER_TYPE_SPECS[server.type].test_hint,
            # A required token can be replaced but not cleared (clearing 422s in writes.py),
            # so the template only offers "Clear" when the type allows an empty secret.
            "secret_clearable": not SERVER_TYPE_SPECS[server.type].requires_secret,
            "library_discovery": registry.get_adapter_class(server.type).supports_library_discovery,
            "webhook_presets": _webhook_preset_options(),
            "webhook_methods": list(HTTP_METHODS),
        },
    )


@router.get("/settings")
async def settings_page(
    request: Request,
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    context = await _status_context(repo, engine)
    return templates.TemplateResponse(request=request, name="settings.html", context=context)


@router.get("/events")
async def events_page(
    request: Request, templates: Jinja2Templates = Depends(get_templates)
) -> Response:
    return templates.TemplateResponse(request=request, name="events.html", context={})


# ---------------------------------------------------------------------------
# /ui form handlers — HTML-partial twins of the JSON /api (contract §J / §K)
# ---------------------------------------------------------------------------


def _split_extensions(raw: str) -> list[str]:
    """Parse a comma/whitespace-separated extensions field into a list.

    Validators in FolderCreate/FolderUpdate normalize and deduplicate further.
    """
    return [part for part in re.split(r"[,\s]+", raw.strip()) if part]


def _parse_folder_rows(form: FormData) -> list[FolderCreate]:
    """Parse the combined add-server form's repeated ``folder-<i>-*`` fields into models.

    Rows are addressed by index (not positional lists) so an unchecked ``enabled`` box can't
    desync the columns. Rows with a blank path are skipped — the form ships one empty row, and
    "add a server with no folders yet" stays valid. FolderCreate validates each kept row.
    """
    indices = sorted(
        {int(m.group(1)) for key in form if (m := re.fullmatch(r"folder-(\d+)-path", key))}
    )
    folders: list[FolderCreate] = []
    for i in indices:
        path = str(form.get(f"folder-{i}-path") or "").strip()
        if not path:
            continue
        library_id = str(form.get(f"folder-{i}-library_id") or "").strip()
        folders.append(
            FolderCreate(
                path=path,
                library_id=library_id or None,
                library_name=str(form.get(f"folder-{i}-library_name") or "").strip() or None,
                extensions=_split_extensions(str(form.get(f"folder-{i}-extensions") or "")),
                enabled=f"folder-{i}-enabled" in form,
            )
        )
    return folders


def _error_partial(
    request: Request, templates: Jinja2Templates, message: str, target: str
) -> Response:
    """Render the inline error partial, retargeted (via htmx headers) into the form's error slot.

    Returns status 200 (htmx only swaps 2xx); the JSON /api surface keeps the real 422.
    """
    response = templates.TemplateResponse(
        request=request, name="_error.html", context={"message": message}
    )
    response.headers["HX-Retarget"] = target
    response.headers["HX-Reswap"] = "innerHTML"
    return response


async def _servers_list_response(
    request: Request, repo: Repo, templates: Jinja2Templates
) -> Response:
    servers = await asyncio.to_thread(repo.list_servers)
    return templates.TemplateResponse(
        request=request, name="_servers_list.html", context={"servers": servers}
    )


def _path_crumbs(path: str) -> list[dict[str, str]]:
    """Breadcrumb segments with cumulative absolute paths: /data/tv → [/, /data, /data/tv]."""
    crumbs = [{"name": "/", "path": "/"}]
    acc = ""
    for part in (p for p in path.split("/") if p):
        acc = f"{acc}/{part}"
        crumbs.append({"name": part, "path": acc})
    return crumbs


def _fs_error_message(exc: OSError) -> str:
    """Map a listing failure to a short, user-facing line (the core raises, the route phrases)."""
    if isinstance(exc, FileNotFoundError):
        return "That folder no longer exists."
    if isinstance(exc, NotADirectoryError):
        return "That path is a file, not a folder."
    if isinstance(exc, PermissionError):
        return "Permission denied reading that folder."
    return "Couldn't read that folder."


@router.get("/ui/fs")
async def ui_browse_fs(
    request: Request,
    path: str = "",
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Read-only directory browser for the folder picker (spec 2026-06-22). Lists immediate
    # subdirectories off the event loop. On any OSError we still render the listing partial in
    # an error state (200, so htmx swaps it) with the breadcrumb to the requested path so the
    # user can climb back out — same "errors render inline" convention as the /ui form handlers.
    requested = os.path.normpath(os.path.abspath(path or "/"))
    listing: DirListing | None = None
    error: str | None = None
    try:
        listing = await asyncio.to_thread(list_directory, path)
    except OSError as exc:
        error = _fs_error_message(exc)
    # Crumbs from the listing's own normalized path on success (single source of truth);
    # fall back to `requested` only in the error branch where there is no listing.
    display_path = listing.path if listing is not None else requested
    return templates.TemplateResponse(
        request=request,
        name="_fs_listing.html",
        context={"listing": listing, "crumbs": _path_crumbs(display_path), "error": error},
    )


@router.post("/ui/servers/new")
async def ui_create_server_with_folders(
    request: Request,
    name: str = Form(...),
    type: str = Form(...),
    base_url: str = Form(""),
    secret: str = Form(""),
    scan_mode: str = Form(...),
    debounce_mode: str = Form(...),
    debounce_window_seconds: int = Form(30),
    retry_attempts: int = Form(3),
    timeout_seconds: float = Form(10.0),
    verify_tls: bool = Form(False),
    enabled: bool = Form(False),
    webhook_method: str = Form(""),
    webhook_headers_json: str = Form(""),
    webhook_body_template: str = Form(""),
    webhook_payload_preset: str = Form("custom"),
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Combined create: one form builds the server AND its folders, written in one transaction.
    # On success we 204 + HX-Redirect to the new detail page; on any failure we render the
    # inline error (retargeted to #form-error) instead of a 500 — same pattern as ui_create_server.
    form = await request.form()
    try:
        server_data = ServerCreate(
            name=name,
            type=ServerType(type),
            base_url=base_url,
            secret=secret or None,
            scan_mode=ScanMode(scan_mode),
            debounce_mode=DebounceMode(debounce_mode),
            debounce_window_seconds=debounce_window_seconds,
            retry_attempts=retry_attempts,
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
            enabled=enabled,
            webhook_method=webhook_method or None,
            webhook_headers_json=webhook_headers_json or None,
            webhook_body_template=webhook_body_template or None,
            webhook_payload_preset=webhook_payload_preset,
            event_types=[FsEventType(cast(str, v)) for v in form.getlist("event_types")],
        )
        folders = _parse_folder_rows(form)
        server = await apply_server_create_with_folders(repo, engine, server_data, folders)
    except HTTPException as exc:
        # incl. the 409 a duplicate name becomes in the write core, and the 422 token gate
        return _error_partial(request, templates, str(exc.detail), "#form-error")
    except ValueError as exc:
        return _error_partial(request, templates, str(exc), "#form-error")
    assert server.id is not None
    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/servers/{server.id}"
    return response


def _test_result_response(
    request: Request, templates: Jinja2Templates, result: ServerTestResponse
) -> Response:
    return templates.TemplateResponse(
        request=request, name="_test_result.html", context={"result": result}
    )


def _library_options_response(
    request: Request, templates: Jinja2Templates, result: LibraryListResult
) -> Response:
    return templates.TemplateResponse(
        request=request, name="_library_options.html", context={"result": result}
    )


@router.post("/ui/servers/test")
async def ui_test_server_config(
    request: Request,
    type: str = Form(...),
    name: str = Form("test"),
    base_url: str = Form(""),
    secret: str = Form(""),
    verify_tls: bool = Form(False),
    timeout_seconds: float = Form(10.0),
    webhook_method: str = Form(""),
    webhook_headers_json: str = Form(""),
    webhook_body_template: str = Form(""),
    webhook_payload_preset: str = Form("custom"),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # "Test before save": probe the UNSAVED config the new-server form currently holds. Builds a
    # throwaway runtime (no DB write) so the create and detail pages can both verify connectivity.
    try:
        data = ServerCreate(
            name=name or "test",
            type=ServerType(type),
            base_url=base_url,
            secret=secret or None,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
            webhook_method=webhook_method or None,
            webhook_headers_json=webhook_headers_json or None,
            webhook_body_template=webhook_body_template or None,
            webhook_payload_preset=webhook_payload_preset,
        )
    except ValueError as exc:
        bad = ServerTestResponse(ok=False, detail=str(exc))
        return _test_result_response(request, templates, bad)
    result = await run_connectivity_test(runtime_from_create(data))
    return _test_result_response(request, templates, result)


@router.post("/ui/servers/{server_id}/test")
async def ui_test_server(
    request: Request,
    server_id: int,
    repo: Repo = Depends(get_repo),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Probe a STORED server (detail page) using its decrypted secret. Same renderer as the
    # form-config test above, so both surfaces show identical results.
    server = await asyncio.to_thread(repo.get_server, server_id)
    if server is None:
        result = ServerTestResponse(ok=False, detail=f"server {server_id} not found")
        return _test_result_response(request, templates, result)
    secret = await asyncio.to_thread(repo.resolve_secret, server)
    result = await run_connectivity_test(runtime_from_server(server, secret))
    return _test_result_response(request, templates, result)


@router.post("/ui/servers/libraries")
async def ui_list_libraries_config(
    request: Request,
    type: str = Form(...),
    base_url: str = Form(""),
    secret: str = Form(""),
    verify_tls: bool = Form(False),
    timeout_seconds: float = Form(10.0),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Unsaved "fetch before save": probe the config the new-server form currently holds.
    try:
        data = ServerCreate(
            name="lib-fetch",
            type=ServerType(type),
            base_url=base_url,
            secret=secret or None,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
        )
    except ValueError as exc:
        return _library_options_response(
            request, templates, LibraryListResult(ok=False, detail=str(exc))
        )
    result = await run_library_listing(runtime_from_create(data))
    return _library_options_response(request, templates, result)


@router.post("/ui/servers/{server_id}/libraries")
async def ui_list_libraries(
    request: Request,
    server_id: int,
    secret: str = Form(""),
    repo: Repo = Depends(get_repo),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Stored path: a freshly-typed token (replace-in-progress) overrides the stored secret;
    # an EMPTY form secret means "fall back to the stored token", never "no auth".
    server = await asyncio.to_thread(repo.get_server, server_id)
    if server is None:
        return _library_options_response(
            request, templates, LibraryListResult(ok=False, detail=f"server {server_id} not found")
        )
    stored = await asyncio.to_thread(repo.resolve_secret, server)
    result = await run_library_listing(runtime_from_server(server, secret or stored))
    return _library_options_response(request, templates, result)


@router.post("/ui/servers/{server_id}/update")
async def ui_update_server(
    request: Request,
    server_id: int,
    name: str = Form(...),
    base_url: str = Form(""),
    secret: str = Form(""),
    clear_secret: bool = Form(False),
    scan_mode: str = Form(...),
    debounce_mode: str = Form(...),
    debounce_window_seconds: int = Form(30),
    retry_attempts: int = Form(3),
    timeout_seconds: float = Form(10.0),
    verify_tls: bool = Form(False),
    enabled: bool = Form(False),
    webhook_method: str = Form(""),
    webhook_headers_json: str = Form(""),
    webhook_body_template: str = Form(""),
    webhook_payload_preset: str = Form("custom"),
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    # Build the schema INSIDE the try: enum/validator failures -> ValueError -> inline error.
    # The folder editor now lives in the SAME form (combined save), so parse its rows here and
    # persist settings + folders atomically. apply_server_update_with_folders raises KeyError if
    # the server was deleted concurrently, HTTPException 422 (secret gate) / 409 (duplicate name).
    form = await request.form()
    try:
        fields: dict[str, Any] = {
            "name": name,
            "base_url": base_url,
            "scan_mode": ScanMode(scan_mode),
            "debounce_mode": DebounceMode(debounce_mode),
            "debounce_window_seconds": debounce_window_seconds,
            "retry_attempts": retry_attempts,
            "timeout_seconds": timeout_seconds,
            "verify_tls": verify_tls,
            "enabled": enabled,
            "webhook_method": webhook_method or None,
            "webhook_headers_json": webhook_headers_json or None,
            "webhook_body_template": webhook_body_template or None,
            "webhook_payload_preset": webhook_payload_preset,
            "event_types": [FsEventType(cast(str, v)) for v in form.getlist("event_types")],
        }
        # Secret tri-state: leave "secret" out of fields to keep the stored token, or set None to
        # clear it; the write-core's exclude_unset dump reads absent=keep, explicit None=clear.
        if clear_secret:
            fields["secret"] = None
        elif secret:
            fields["secret"] = secret
        data = ServerUpdate(**fields)
        folders = _parse_folder_rows(form)
        await apply_server_update_with_folders(repo, engine, server_id, data, folders)
    except HTTPException as exc:
        return _error_partial(request, templates, str(exc.detail), "#save-error")
    except (ValueError, KeyError) as exc:
        return _error_partial(request, templates, str(exc), "#save-error")
    return templates.TemplateResponse(
        request=request, name="_saved.html", context={"message": "Saved."}
    )


@router.post("/ui/servers/{server_id}/delete")
async def ui_delete_server(
    request: Request,
    server_id: int,
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    try:
        await apply_server_delete(repo, engine, server_id)
    except (HTTPException, ValueError, KeyError) as exc:
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        return _error_partial(request, templates, detail, "#edit-error")
    return await _servers_list_response(request, repo, templates)


_INOTIFY_GATE_VALUES = frozenset({"enforce", "off"})


@router.post("/ui/settings")
async def ui_settings(
    request: Request,
    inotify_gate: str = Form(...),
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    if inotify_gate not in _INOTIFY_GATE_VALUES:
        raise HTTPException(status_code=422, detail="inotify_gate must be 'enforce' or 'off'")
    await asyncio.to_thread(repo.set_setting, "inotify_gate", inotify_gate)
    await rebuild_engine(engine)  # flipping to off can recover a blocked engine (§H/§I)
    context = await _status_context(repo, engine)
    return templates.TemplateResponse(request=request, name="_status.html", context=context)


@router.post("/ui/recheck")
async def ui_recheck(
    request: Request,
    repo: Repo = Depends(get_repo),
    engine: Engine = Depends(get_engine),
    templates: Jinja2Templates = Depends(get_templates),
) -> Response:
    await rebuild_engine(engine)  # re-evaluate the gate after an out-of-band host limit change (§H)
    context = await _status_context(repo, engine)
    return templates.TemplateResponse(request=request, name="_status.html", context=context)
