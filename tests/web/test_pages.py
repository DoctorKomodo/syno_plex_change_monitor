"""Page routes: 200 for an authed client, 303 redirect for an anon client, key content present."""

import httpx

from mediascanmonitor.db.models import ServerType
from mediascanmonitor.db.schemas import FolderCreate, ServerCreate
from mediascanmonitor.engine import EngineState
from mediascanmonitor.watcher.watch_limit import WatchLimitStatus


def _seed_server(repo) -> int:  # type: ignore[no-untyped-def]
    server = repo.create_server(
        ServerCreate(
            name="Plex Main", type=ServerType.plex, base_url="http://plex:32400", secret="tok"
        )
    )
    repo.create_folder(server.id, FolderCreate(path="/data/tv", library_id="2", extensions=["mkv"]))
    return int(server.id)


def test_dashboard_redirects_when_anon(client: httpx.Client) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    # No password set on the `client` fixture → require_page_auth routes to first-run /setup
    # (it targets /login only once a password exists).
    assert resp.headers["location"] == "/setup"


def test_dashboard_renders_engine_and_watch_status(auth_client: httpx.Client, engine) -> None:  # type: ignore[no-untyped-def]
    engine.state = EngineState.blocked
    engine.watch_limit = WatchLimitStatus(
        current=8192, dirs=20000, needed=24000, recommended=28800, ok=False
    )
    resp = auth_client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "blocked" in body
    assert "28800" in body  # recommended kernel ceiling surfaced
    assert "max_user_watches" in body  # the recommended sysctl line


def test_servers_page_lists_servers_and_links_to_add(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    _seed_server(repo)
    resp = auth_client.get("/servers")
    assert resp.status_code == 200
    assert "Plex Main" in resp.text
    assert "/servers/new" in resp.text  # add now lives on its own page, linked from here


def test_server_new_page_has_server_and_folder_fields(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/servers/new")
    assert resp.status_code == 200
    assert 'name="type"' in resp.text  # the server form
    assert 'name="folder-0-path"' in resp.text  # the first (combined) folder row
    assert "/ui/servers/test" in resp.text  # the "test before save" button


def test_server_detail_and_new_share_folder_add_and_form(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    sid = _seed_server(repo)
    detail = auth_client.get(f"/servers/{sid}").text
    new = auth_client.get("/servers/new").text
    # Both pages render the same shared field sections and the same batch folder-add component.
    for marker in ("Reliability", 'name="folder-0-path"', "data-folder-editor"):
        assert marker in detail and marker in new


def test_webhook_fields_only_shown_for_webhook_servers(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    plex = _seed_server(repo)  # a plex server
    hook = repo.create_server(ServerCreate(name="hook", type=ServerType.webhook))
    assert 'name="webhook_method"' not in auth_client.get(f"/servers/{plex}").text
    assert 'name="webhook_method"' in auth_client.get(f"/servers/{hook.id}").text


def test_webhook_method_is_a_select_with_stored_value_preselected(auth_client, repo) -> None:  # type: ignore[no-untyped-def]
    hook = repo.create_server(
        ServerCreate(name="hook", type=ServerType.webhook, webhook_method="PUT")
    )
    text = auth_client.get(f"/servers/{hook.id}").text
    # Method is a constrained dropdown now, not a free-text input, with the stored verb selected.
    assert '<select name="webhook_method">' in text
    assert '<option value="PUT" selected>PUT</option>' in text


def test_base_url_label_is_endpoint_for_webhook(auth_client, repo) -> None:  # type: ignore[no-untyped-def]
    plex = _seed_server(repo)
    hook = repo.create_server(
        ServerCreate(name="hook", type=ServerType.webhook, base_url="https://h")
    )
    assert "Endpoint URL" in auth_client.get(f"/servers/{hook.id}").text
    assert "Endpoint URL" not in auth_client.get(f"/servers/{plex}").text
    assert "Base URL" in auth_client.get(f"/servers/{plex}").text


def test_token_and_test_hints_are_per_type(auth_client, repo) -> None:  # type: ignore[no-untyped-def]
    plex = _seed_server(repo)
    hook = repo.create_server(
        ServerCreate(name="hook", type=ServerType.webhook, base_url="https://h")
    )
    plex_text = auth_client.get(f"/servers/{plex}").text
    hook_text = auth_client.get(f"/servers/{hook.id}").text
    # Webhook explains the token is optional and referenceable in templates as {{ secret }}.
    assert "{{ secret }}" in hook_text
    assert "{{ secret }}" not in plex_text
    # Test-button hint reflects what the test does for each backend.
    assert "Sends a test event to the endpoint." in hook_text
    assert "Checks the URL and token work." in plex_text


def test_server_detail_shows_folders_and_test_button(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    sid = _seed_server(repo)
    resp = auth_client.get(f"/servers/{sid}")
    assert resp.status_code == 200
    assert "Test" in resp.text  # Test button present
    # The existing folder is pre-loaded into the unified editor, and settings + folders now
    # save together via ONE form posting to /update (no separate "Save folders" form).
    assert 'value="/data/tv"' in resp.text
    assert "data-folder-editor" in resp.text
    assert f"/ui/servers/{sid}/update" in resp.text  # the one consolidated save form
    assert f"/ui/servers/{sid}/folders" not in resp.text  # separate folder-sync form is gone
    assert "Save changes" in resp.text
    # Deleting is done from the servers list ("Remove"), not the detail page.
    assert "Delete server" not in resp.text


def test_server_detail_404_for_missing(auth_client: httpx.Client) -> None:
    assert auth_client.get("/servers/9999").status_code == 404


def test_settings_page_has_gate_toggle(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert 'name="inotify_gate"' in resp.text
    assert "Re-check" in resp.text


def test_events_page_opens_stream(auth_client: httpx.Client) -> None:
    resp = auth_client.get("/events")
    assert resp.status_code == 200
    assert "/events/stream" in resp.text  # the page wires the SSE source


def test_pages_redirect_when_anon(client: httpx.Client) -> None:
    for path in ("/servers", "/settings", "/events"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path


def test_ui_fs_lists_subdirs_for_authed(auth_client: httpx.Client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "movies").mkdir()
    (tmp_path / "tv").mkdir()
    (tmp_path / "note.txt").write_text("x")
    resp = auth_client.get("/ui/fs", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert "movies" in resp.text and "tv" in resp.text
    assert "note.txt" not in resp.text
    assert f'data-current-path="{tmp_path}"' in resp.text  # the JS hook the picker reads


def test_ui_fs_bad_path_renders_inline_error_not_500(auth_client: httpx.Client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    resp = auth_client.get("/ui/fs", params={"path": str(tmp_path / "missing")})
    assert resp.status_code == 200  # rendered inline so htmx can swap it, never a 500
    assert "no longer exists" in resp.text.lower()


def test_ui_fs_normalizes_dotdot(auth_client: httpx.Client, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "a").mkdir()
    resp = auth_client.get("/ui/fs", params={"path": str(tmp_path / "a" / "..")})
    assert resp.status_code == 200
    assert f'data-current-path="{tmp_path}"' in resp.text


def test_ui_fs_redirects_when_anon(client: httpx.Client) -> None:
    r = client.get("/ui/fs", params={"path": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_ui_fs_root_crumb_is_a_navigable_link_not_an_inert_label(auth_client: httpx.Client) -> None:
    # The root "/" crumb must read as a navigation target in every state. At root it is the leaf,
    # but unlike a leaf folder it stays a clickable crumb (it used to render as the inert white
    # "you are here" label only at root, which flipped its colour vs. subdirs).
    resp = auth_client.get("/ui/fs", params={"path": "/"})
    assert resp.status_code == 200
    assert 'class="fs-crumb"' in resp.text  # the root crumb is the clickable button form
    assert "fs-crumb-here" not in resp.text  # nothing is the inert current-location label at root


def test_ui_fs_root_crumb_has_no_redundant_leading_separator(
    auth_client: httpx.Client, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    # The root "/" is itself the leading divider, so it must not be followed by a separator slash —
    # that rendered a "//" prefix (e.g. "//home/asg"). Only the root crumb's label is "/", so a
    # root-button immediately followed by a separator span is exactly the regression.
    resp = auth_client.get("/ui/fs", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert '>/</button><span class="fs-crumb-sep">' not in resp.text


def test_folder_picker_present_on_new_and_detail(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    sid = _seed_server(repo)
    for body in (auth_client.get("/servers/new").text, auth_client.get(f"/servers/{sid}").text):
        assert "data-browse" in body  # the per-row Browse button
        assert "data-folder-picker" in body  # the shared dialog shell
        assert 'id="fs-listing"' in body  # the htmx swap target inside the dialog
        assert "data-picker-select" in body  # the Select control


def test_new_page_exposes_discovery_in_type_specs(auth_client: httpx.Client) -> None:
    body = auth_client.get("/servers/new").text
    assert "supports_library_discovery" in body  # serialized into #type-specs for the per-type JS
    assert "data-fetch-lib" in body  # the Fetch button is always rendered (JS/CSS gate visibility)


def test_abs_detail_flags_library_discovery_on(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    server = repo.create_server(
        ServerCreate(
            name="ABS", type=ServerType.audiobookshelf, base_url="http://abs:13378", secret="t"
        )
    )
    repo.create_folder(
        server.id,
        FolderCreate(
            path="/data/abs", library_id="lib_x", library_name="Audiobooks", extensions=["m4b"]
        ),
    )
    body = auth_client.get(f"/servers/{server.id}").text
    assert 'data-library-discovery="true"' in body
    assert "data-fetch-lib" in body
    assert "Audiobooks" in body  # the saved friendly name renders as the companion label


def test_plex_detail_flags_library_discovery_on(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    sid = _seed_server(repo)  # a plex server
    body = auth_client.get(f"/servers/{sid}").text
    assert 'data-library-discovery="true"' in body
    assert "data-fetch-lib" in body


def test_webhook_detail_flags_library_discovery_off(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    hook = repo.create_server(ServerCreate(name="hook", type=ServerType.webhook))
    body = auth_client.get(f"/servers/{hook.id}").text
    assert 'data-library-discovery="false"' in body


def test_server_read_carries_webhook_payload_preset(repo) -> None:  # type: ignore[no-untyped-def]
    from mediascanmonitor.db.models import WebhookPreset
    from mediascanmonitor.web.api_schemas import ServerRead

    server = repo.create_server(
        ServerCreate(
            name="hook-read",
            type=ServerType.webhook,
            webhook_payload_preset=WebhookPreset.sonarr_radarr,
        )
    )
    read = ServerRead.from_model(server, [])
    assert read.webhook_payload_preset == WebhookPreset.sonarr_radarr


def test_webhook_form_renders_payload_preset_select(auth_client: httpx.Client) -> None:
    body = auth_client.get("/servers/new").text
    assert 'name="webhook_payload_preset"' in body
    assert "Sonarr / Radarr" in body  # the registry label is offered


def test_webhook_detail_preselects_saved_preset(auth_client: httpx.Client, repo) -> None:  # type: ignore[no-untyped-def]
    import re

    from mediascanmonitor.db.models import WebhookPreset

    hook = repo.create_server(
        ServerCreate(
            name="hook-pre",
            type=ServerType.webhook,
            webhook_payload_preset=WebhookPreset.sonarr_radarr,
        )
    )
    body = auth_client.get(f"/servers/{hook.id}").text
    # the saved preset's <option> is the selected one
    assert re.search(r'value="sonarr_radarr"[^>]*\bselected\b', body)


def test_webhook_preset_toggle_script_present(auth_client: httpx.Client) -> None:
    body = auth_client.get("/servers/new").text
    assert "webhook-preset-select" in body  # the select id the toggle script binds to
    assert "field-webhook-body" in body  # the element it shows/hides


def test_new_server_form_renders_all_four_event_type_checkboxes_checked(
    auth_client: httpx.Client,
) -> None:
    import re

    body = auth_client.get("/servers/new").text
    for value in ("created", "moved_to", "deleted", "moved_from"):
        assert re.search(rf'name="event_types" value="{value}"[^>]*\bchecked\b', body), value


def test_server_detail_preselects_saved_event_types(
    auth_client: httpx.Client, repo  # type: ignore[no-untyped-def]
) -> None:
    import re

    from mediascanmonitor.db.models import FsEventType

    server = repo.create_server(
        ServerCreate(
            name="hook-events",
            type=ServerType.webhook,
            event_types=[FsEventType.created, FsEventType.deleted],
        )
    )
    body = auth_client.get(f"/servers/{server.id}").text
    assert re.search(r'name="event_types" value="created"[^>]*\bchecked\b', body)
    assert re.search(r'name="event_types" value="deleted"[^>]*\bchecked\b', body)
    assert not re.search(r'name="event_types" value="moved_to"[^>]*\bchecked\b', body)
    assert not re.search(r'name="event_types" value="moved_from"[^>]*\bchecked\b', body)
