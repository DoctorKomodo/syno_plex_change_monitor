"""/ui form handlers: success swaps a partial, 422 renders inline, the write-core rebuilds."""

import httpx
import respx

from mediascanmonitor.db.models import ServerType
from mediascanmonitor.db.schemas import FolderCreate, ServerCreate


def _seed_plex(repo) -> int:  # type: ignore[no-untyped-def]
    s = repo.create_server(
        ServerCreate(name="Plex", type=ServerType.plex, base_url="http://plex:32400", secret="tok")
    )
    return int(s.id)


def test_ui_create_server_with_folders_redirects_and_persists(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    before = engine.rebuild_calls
    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "Plex Combined",
            "type": "plex",
            "base_url": "http://plex:32400",
            "secret": "tok",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "verify_tls": "on",
            "enabled": "on",
            "folder-0-path": "/data/tv",
            "folder-0-library_id": "2",
            "folder-0-extensions": "mkv, mp4",
            "folder-0-enabled": "on",
            # a blank row is skipped, not an error
            "folder-1-path": "",
            "folder-2-path": "/data/movies",
            "folder-2-extensions": "mkv",
        },
    )
    assert resp.status_code == 204
    created = next(s for s in repo.list_servers() if s.name == "Plex Combined")
    assert resp.headers["hx-redirect"] == f"/servers/{created.id}"
    assert engine.rebuild_calls == before + 1  # one rebuild for the whole create
    folders = repo.list_folders(created.id)
    assert {f.path for f in folders} == {"/data/tv", "/data/movies"}  # blank row skipped
    by_path = {f.path: f for f in folders}
    assert sorted(ft.extension for ft in by_path["/data/tv"].filetypes) == ["mkv", "mp4"]
    # Index-addressed rows: folder-0 sent enabled=on, folder-2 omitted the checkbox entirely.
    # The omitted box must land enabled=False (the desync the index scheme guards against).
    assert by_path["/data/tv"].enabled is True
    assert by_path["/data/movies"].enabled is False


def test_ui_create_server_persists_narrowed_event_types(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "Webhook Created Only",
            "type": "webhook",
            "scan_mode": "library",
            "debounce_mode": "off",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "event_types": ["created", "moved_to"],
            "folder-0-path": "",
        },
    )
    assert resp.status_code == 204
    created = next(s for s in repo.list_servers() if s.name == "Webhook Created Only")
    assert created.event_types == "created,moved_to"


def test_ui_create_server_with_no_event_types_checked_saves_empty(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "No Events Yet",
            "type": "webhook",
            "scan_mode": "library",
            "debounce_mode": "off",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            # no "event_types" key: every checkbox unchecked
            "folder-0-path": "",
        },
    )
    assert resp.status_code == 204
    created = next(s for s in repo.list_servers() if s.name == "No Events Yet")
    assert created.event_types == ""


def test_ui_create_server_with_folders_rejects_missing_token_atomically(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    before = engine.rebuild_calls
    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "NoTokenPlex",
            "type": "plex",
            "base_url": "http://plex:32400",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "folder-0-path": "/data/tv",
        },
    )
    assert resp.status_code == 200  # softened so htmx swaps the inline error
    assert "token" in resp.text.lower()
    assert resp.headers.get("hx-retarget") == "#form-error"
    assert engine.rebuild_calls == before  # nothing written
    assert not any(s.name == "NoTokenPlex" for s in repo.list_servers())  # no orphan server/folders


def test_ui_create_server_with_folders_duplicate_name_renders_inline_error(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    _seed_plex(repo)  # an existing server named "Plex"
    before = engine.rebuild_calls
    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "Plex",  # collides
            "type": "plex",
            "base_url": "http://plex:32400",
            "secret": "tok",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "folder-0-path": "/data/tv",
        },
    )
    assert resp.status_code == 200  # softened so htmx swaps the message
    assert "already exists" in resp.text.lower()
    assert resp.headers.get("hx-retarget") == "#form-error"
    assert engine.rebuild_calls == before  # nothing rebuilt
    assert len(repo.list_servers()) == 1  # the duplicate (and its folder) never landed


def test_ui_update_server_keeps_secret_when_blank_and_rebuilds(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    before = engine.rebuild_calls
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "Plex",
            "base_url": "http://plex:32400",
            "secret": "",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "verify_tls": "on",
            "enabled": "on",
        },
    )
    assert resp.status_code == 200
    assert engine.rebuild_calls == before + 1
    assert repo.get_server(sid).secret_encrypted is not None  # blank secret left the token intact


def test_ui_delete_server_swaps_list_and_rebuilds(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    before = engine.rebuild_calls
    resp = auth_client.post(f"/ui/servers/{sid}/delete")
    assert resp.status_code == 200
    assert engine.rebuild_calls == before + 1
    assert repo.get_server(sid) is None


def test_ui_update_persists_webhook_fields(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    server = repo.create_server(
        ServerCreate(name="hook", type=ServerType.webhook, webhook_method="POST")
    )
    sid = int(server.id)
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "hook",
            "scan_mode": "library",
            "debounce_mode": "off",
            "debounce_window_seconds": "30",
            "retry_attempts": "1",
            "timeout_seconds": "10",
            "webhook_method": "PUT",
            "webhook_headers_json": '{"X-Token": "abc"}',
            "webhook_body_template": '{"path": "x"}',
        },
    )
    assert resp.status_code == 200
    updated = repo.get_server(sid)
    assert updated.webhook_method == "PUT"  # webhook fields now editable on the detail page
    assert updated.webhook_headers_json == '{"X-Token": "abc"}'


@respx.mock
def test_ui_test_stored_server_renders_result(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    server = repo.create_server(
        ServerCreate(name="emby", type=ServerType.emby, base_url="http://emby:8096", secret="t")
    )
    respx.get("http://emby:8096/System/Info").mock(return_value=httpx.Response(200))
    resp = auth_client.post(f"/ui/servers/{server.id}/test")
    assert resp.status_code == 200
    assert "test-ok" in resp.text  # shared _test_result.html, success styling


@respx.mock
def test_ui_test_form_config_renders_result_without_saving(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    respx.get("http://emby:8096/System/Info").mock(return_value=httpx.Response(401))
    resp = auth_client.post(
        "/ui/servers/test",
        data={"type": "emby", "base_url": "http://emby:8096", "secret": "t", "verify_tls": "on"},
    )
    assert resp.status_code == 200
    assert "test-fail" in resp.text  # 401 -> failure
    assert repo.list_servers() == []  # test-before-save never persists


def test_ui_delete_server_nonexistent_returns_200(
    auth_client: httpx.Client,
) -> None:
    """Deleting a non-existent server returns 200 (not 500) — idempotent delete."""
    resp = auth_client.post("/ui/servers/9999/delete")
    assert resp.status_code == 200


def test_ui_update_saves_fields_and_folders_together(
    auth_client: httpx.Client,
    repo,
    engine,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    repo.create_folder(sid, FolderCreate(path="/old", extensions=["mkv"]))  # replaced wholesale
    before = engine.rebuild_calls
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "Plex Renamed",
            "base_url": "http://plex:32400",
            "secret": "",  # blank keeps the stored token
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "verify_tls": "on",
            "enabled": "on",
            "folder-0-path": "/data/tv",
            "folder-0-library_id": "2",
            "folder-0-extensions": "mkv, mp4",
            "folder-0-enabled": "on",
            "folder-1-path": "",  # blank row skipped
            "folder-2-path": "/data/movies",
            "folder-2-extensions": "mkv",
        },
    )
    assert resp.status_code == 200
    assert "saved" in resp.text.lower()  # _saved.html rendered into #save-status
    assert engine.rebuild_calls == before + 1  # one rebuild for the whole save
    saved = repo.get_server(sid)
    assert saved.name == "Plex Renamed"
    assert saved.secret_encrypted is not None  # blank secret left the token intact
    assert {f.path for f in repo.list_folders(sid)} == {"/data/tv", "/data/movies"}  # /old gone


def test_ui_update_replaces_event_types_from_checked_boxes(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "Plex",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "event_types": ["deleted"],
            "folder-0-path": "",
        },
    )
    assert resp.status_code == 200
    assert repo.get_server(sid).event_types == "deleted"


def test_ui_update_unchecking_all_event_types_clears_them(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "Plex",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            # no "event_types" key: every checkbox unchecked, same as a real all-unchecked submit
            "folder-0-path": "",
        },
    )
    assert resp.status_code == 200
    assert repo.get_server(sid).event_types == ""


def test_ui_update_empty_folder_rows_clears_all(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    sid = _seed_plex(repo)
    repo.create_folder(sid, FolderCreate(path="/data/tv", extensions=["mkv"]))
    resp = auth_client.post(
        f"/ui/servers/{sid}/update",
        data={
            "name": "Plex",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "folder-0-path": "",  # all blank → no folders
        },
    )
    assert resp.status_code == 200
    assert repo.list_folders(sid) == []  # an all-blank save is a valid "no folders"


def test_ui_update_missing_server_returns_200_inline_error(
    auth_client: httpx.Client,
) -> None:
    resp = auth_client.post(
        "/ui/servers/9999/update",
        data={
            "name": "ghost",
            "scan_mode": "targeted",
            "debounce_mode": "trailing",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "folder-0-path": "/data/tv",
        },
    )
    assert resp.status_code == 200  # softened so htmx swaps the message
    assert resp.headers.get("hx-retarget") == "#save-error"


def test_ui_create_webhook_persists_payload_preset(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
    engine,  # type: ignore[no-untyped-def]
) -> None:
    from mediascanmonitor.db.models import WebhookPreset

    resp = auth_client.post(
        "/ui/servers/new",
        data={
            "name": "Hook Preset",
            "type": "webhook",
            "scan_mode": "library",
            "debounce_mode": "off",
            "debounce_window_seconds": "30",
            "retry_attempts": "3",
            "timeout_seconds": "10",
            "webhook_payload_preset": "sonarr_radarr",
            "folder-0-path": "/data/tv",
            "folder-0-extensions": "mkv",
            "folder-0-enabled": "on",
        },
    )
    assert resp.status_code == 204
    created = next(s for s in repo.list_servers() if s.name == "Hook Preset")
    assert created.webhook_payload_preset == WebhookPreset.sonarr_radarr


def test_ui_update_duplicate_name_returns_inline_409(
    auth_client: httpx.Client,
    repo,  # type: ignore[no-untyped-def]
) -> None:
    _seed_plex(repo)  # name "Plex"
    other = repo.create_server(ServerCreate(name="Other", type=ServerType.webhook))
    oid = int(other.id)
    resp = auth_client.post(
        f"/ui/servers/{oid}/update",
        data={
            "name": "Plex",  # collides with the seeded server
            "scan_mode": "library",
            "debounce_mode": "off",
            "debounce_window_seconds": "30",
            "retry_attempts": "1",
            "timeout_seconds": "10",
            "webhook_method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("hx-retarget") == "#save-error"
    assert "already exists" in resp.text.lower()
    assert repo.get_server(oid).name == "Other"  # nothing persisted (rolled back)
