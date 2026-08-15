"""Tests for the Repo CRUD/crypto contract (contract section 4)."""

from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from mediascanmonitor.db.models import FileType, Folder, ServerType
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.db.schemas import FolderCreate, ServerCreate, ServerUpdate


def make_server(
    name: str = "plex1", *, enabled: bool = True, secret: str | None = "tok"
) -> ServerCreate:
    return ServerCreate(
        name=name,
        type=ServerType.plex,
        base_url="https://plex:32400",
        secret=secret,
        enabled=enabled,
    )


def test_create_server_encrypts_secret(repo: Repo) -> None:
    server = repo.create_server(make_server(secret="my-token"))
    assert server.id is not None
    assert server.secret_encrypted is not None
    assert server.secret_encrypted != "my-token"
    assert repo.resolve_secret(server) == "my-token"


def test_create_server_without_secret(repo: Repo) -> None:
    server = repo.create_server(make_server(secret=None))
    assert server.secret_encrypted is None
    assert repo.resolve_secret(server) is None


def test_create_server_with_folders_persists_both(repo: Repo) -> None:
    server = repo.create_server_with_folders(
        make_server(name="combined"),
        [
            FolderCreate(path="/data/tv", library_id="2", extensions=["mkv", "MP4", "mkv"]),
            FolderCreate(path="/data/movies", extensions=[]),
        ],
    )
    assert server.id is not None
    folders = repo.list_folders(server.id)
    assert {f.path for f in folders} == {"/data/tv", "/data/movies"}
    tv = next(f for f in folders if f.path == "/data/tv")
    assert sorted(ft.extension for ft in tv.filetypes) == ["mkv", "mp4"]  # normalized + deduped


def test_create_server_defaults_event_types_to_all_four(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType

    server = repo.create_server(make_server(name="all-events"))
    assert server.event_types == "created,moved_to,deleted,moved_from"
    assert server.event_types == ",".join(t.value for t in FsEventType)


def test_create_server_persists_narrowed_event_types(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType
    from mediascanmonitor.db.schemas import ServerCreate as SC

    created = repo.create_server(
        SC(
            name="hook-narrow",
            type=ServerType.webhook,
            event_types=[FsEventType.created, FsEventType.moved_to],
        )
    )
    assert created.event_types == "created,moved_to"


def test_create_server_with_folders_persists_event_types(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType
    from mediascanmonitor.db.schemas import ServerCreate as SC

    server = repo.create_server_with_folders(
        SC(name="combined-events", type=ServerType.plex, event_types=[FsEventType.deleted]),
        [],
    )
    assert server.event_types == "deleted"


def test_update_server_with_folders_changes_fields_and_swaps_folders(repo: Repo) -> None:
    server = repo.create_server_with_folders(
        make_server(name="combo"), [FolderCreate(path="/old", extensions=["avi"])]
    )
    assert server.id is not None
    updated = repo.update_server_with_folders(
        server.id,
        ServerUpdate(enabled=False),
        [
            FolderCreate(path="/data/tv", extensions=["mkv", "MP4"]),
            FolderCreate(path="/data/movies", extensions=["mkv"]),
        ],
    )
    assert updated.enabled is False
    folders = repo.list_folders(server.id)
    assert {f.path for f in folders} == {"/data/tv", "/data/movies"}  # /old replaced wholesale
    tv = next(f for f in folders if f.path == "/data/tv")
    assert sorted(ft.extension for ft in tv.filetypes) == ["mkv", "mp4"]  # normalized + deduped


def test_update_server_with_folders_narrows_event_types(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType

    server = repo.create_server(make_server(name="combo"))
    assert server.id is not None
    updated = repo.update_server_with_folders(
        server.id, ServerUpdate(event_types=[FsEventType.moved_from]), []
    )
    assert updated.event_types == "moved_from"


def test_update_server_with_folders_empty_clears_all(repo: Repo) -> None:
    server = repo.create_server_with_folders(
        make_server(name="clearfolders"), [FolderCreate(path="/x", extensions=["mkv"])]
    )
    assert server.id is not None
    repo.update_server_with_folders(server.id, ServerUpdate(), [])
    assert repo.list_folders(server.id) == []


def test_update_server_with_folders_unknown_server_raises(repo: Repo) -> None:
    with pytest.raises(KeyError):
        repo.update_server_with_folders(
            9999, ServerUpdate(), [FolderCreate(path="/data/tv", extensions=["mkv"])]
        )


def test_create_server_with_folders_is_atomic_on_duplicate_name(repo: Repo) -> None:
    existing = repo.create_server(make_server(name="dupe"))
    assert existing.id is not None
    with pytest.raises(IntegrityError):
        repo.create_server_with_folders(
            make_server(name="dupe"), [FolderCreate(path="/data/tv", extensions=["mkv"])]
        )
    # The whole transaction rolled back: no second server was added, and the folder that would
    # have been created went with it (a committed orphan is impossible — folders FK to a server).
    assert len(repo.list_servers()) == 1
    assert repo.list_folders(existing.id) == []


def test_get_server_round_trip_and_missing(repo: Repo) -> None:
    created = repo.create_server(make_server())
    assert created.id is not None
    fetched = repo.get_server(created.id)
    assert fetched is not None
    assert fetched.name == "plex1"
    assert repo.get_server(9999) is None


def test_list_servers_enabled_only(repo: Repo) -> None:
    repo.create_server(make_server(name="on", enabled=True))
    repo.create_server(make_server(name="off", enabled=False))
    assert len(repo.list_servers()) == 2
    enabled = repo.list_servers(enabled_only=True)
    assert [s.name for s in enabled] == ["on"]


def test_update_server_changes_fields_and_keeps_secret(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    updated = repo.update_server(
        server.id, ServerUpdate(base_url="https://new:32400", enabled=False)
    )
    assert updated.base_url == "https://new:32400"
    assert updated.enabled is False
    assert repo.resolve_secret(updated) == "tok"  # secret untouched


def test_update_server_reencrypts_secret(repo: Repo) -> None:
    server = repo.create_server(make_server(secret="old"))
    assert server.id is not None
    old_ciphertext = server.secret_encrypted
    updated = repo.update_server(server.id, ServerUpdate(secret="new"))
    assert updated.secret_encrypted != old_ciphertext
    assert repo.resolve_secret(updated) == "new"


def test_update_server_clears_secret_when_explicitly_none(repo: Repo) -> None:
    # explicit secret=None clears the stored credential (distinct from omitting it)
    server = repo.create_server(make_server(secret="tok"))
    assert server.id is not None
    assert server.secret_encrypted is not None
    updated = repo.update_server(server.id, ServerUpdate(secret=None))
    assert updated.secret_encrypted is None
    assert repo.resolve_secret(updated) is None


def test_update_server_narrows_event_types(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType

    server = repo.create_server(make_server())
    assert server.id is not None
    updated = repo.update_server(server.id, ServerUpdate(event_types=[FsEventType.created]))
    assert updated.event_types == "created"


def test_update_server_empty_event_types_clears(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    updated = repo.update_server(server.id, ServerUpdate(event_types=[]))
    assert updated.event_types == ""


def test_update_server_omitted_event_types_unchanged(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    before = server.event_types
    updated = repo.update_server(server.id, ServerUpdate(base_url="https://new:32400"))
    assert updated.event_types == before


def test_update_server_explicit_none_event_types_is_noop(repo: Repo) -> None:
    # Mirrors update_folder's "explicit None is a no-op" contract for extensions (contract
    # section 4) — a JSON PATCH sending `{"event_types": null}` must not crash or clear anything.
    server = repo.create_server(make_server())
    assert server.id is not None
    before = server.event_types
    updated = repo.update_server(server.id, ServerUpdate(event_types=None))
    assert updated.event_types == before


def test_delete_server_cascades_to_folders_and_filetypes(
    repo: Repo, factory: Callable[[], Session]
) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    repo.create_folder(
        server.id,
        FolderCreate(path="/data/tv", library_id="2", extensions=["mkv", "srt"]),
    )
    repo.delete_server(server.id)
    assert repo.get_server(server.id) is None
    assert repo.list_folders(server.id) == []
    with factory() as session:
        assert list(session.exec(select(Folder)).all()) == []
        assert list(session.exec(select(FileType)).all()) == []


def test_create_folder_normalizes_path_and_extensions(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    folder = repo.create_folder(
        server.id, FolderCreate(path="/data/tv/", extensions=[".MKV", " Srt "])
    )
    assert folder.path == "/data/tv"
    assert {ft.extension for ft in folder.filetypes} == {"mkv", "srt"}


def test_list_folders_returns_filetypes(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    repo.create_folder(server.id, FolderCreate(path="/data/tv", extensions=["mkv"]))
    folders = repo.list_folders(server.id)
    assert len(folders) == 1
    assert {ft.extension for ft in folders[0].filetypes} == {"mkv"}


def test_delete_folder(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    folder = repo.create_folder(server.id, FolderCreate(path="/data/tv"))
    assert folder.id is not None
    repo.delete_folder(folder.id)
    assert repo.list_folders(server.id) == []


def test_set_filetypes_replaces_wholesale_and_normalizes(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    folder = repo.create_folder(server.id, FolderCreate(path="/data/tv", extensions=["mkv", "mp4"]))
    assert folder.id is not None
    result = repo.set_filetypes(folder.id, [".SRT"])
    assert [ft.extension for ft in result] == ["srt"]
    folders = repo.list_folders(server.id)
    assert {ft.extension for ft in folders[0].filetypes} == {"srt"}


def test_set_filetypes_empty_list_means_all(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    folder = repo.create_folder(server.id, FolderCreate(path="/data/tv", extensions=["mkv"]))
    assert folder.id is not None
    result = repo.set_filetypes(folder.id, [])
    assert result == []
    folders = repo.list_folders(server.id)
    assert folders[0].filetypes == []


def test_settings_get_and_set(repo: Repo) -> None:
    assert repo.get_setting("missing") is None
    repo.set_setting("password_hash", "abc")
    assert repo.get_setting("password_hash") == "abc"
    repo.set_setting("password_hash", "def")  # overwrite
    assert repo.get_setting("password_hash") == "def"


def test_create_folder_unknown_server_raises(repo: Repo) -> None:
    # FK enforcement (PRAGMA foreign_keys=ON): a dangling server_id is rejected, not orphaned
    with pytest.raises(IntegrityError):
        repo.create_folder(9999, FolderCreate(path="/data/tv"))


def test_update_server_unknown_raises(repo: Repo) -> None:
    with pytest.raises(KeyError):
        repo.update_server(9999, ServerUpdate(enabled=False))


def test_set_filetypes_unknown_folder_raises(repo: Repo) -> None:
    with pytest.raises(KeyError):
        repo.set_filetypes(9999, ["mkv"])


def test_delete_missing_is_idempotent(repo: Repo) -> None:
    repo.delete_server(9999)  # no raise
    repo.delete_folder(9999)  # no raise


def test_create_folder_persists_library_name(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    repo.create_folder(
        server.id,
        FolderCreate(path="/data/abs", library_id="lib_x", library_name="Audiobooks"),
    )
    [folder] = repo.list_folders(server.id)
    assert (folder.library_id, folder.library_name) == ("lib_x", "Audiobooks")


def test_create_folder_defaults_library_name_to_none(repo: Repo) -> None:
    server = repo.create_server(make_server())
    assert server.id is not None
    repo.create_folder(server.id, FolderCreate(path="/data/tv", library_id="2"))
    [folder] = repo.list_folders(server.id)
    assert folder.library_name is None


def test_update_server_with_folders_persists_library_name(repo: Repo) -> None:
    server = repo.create_server_with_folders(make_server(name="abs"), [])
    assert server.id is not None
    repo.update_server_with_folders(
        server.id,
        ServerUpdate(),
        [FolderCreate(path="/data/pods", library_id="lib_y", library_name="Podcasts")],
    )
    [folder] = repo.list_folders(server.id)
    assert (folder.library_id, folder.library_name) == ("lib_y", "Podcasts")


def test_create_server_persists_webhook_payload_preset(repo: Repo) -> None:
    from mediascanmonitor.db.models import WebhookPreset
    from mediascanmonitor.db.schemas import ServerCreate as SC

    created = repo.create_server(
        SC(
            name="hook-sr",
            type=ServerType.webhook,
            webhook_payload_preset=WebhookPreset.sonarr_radarr,
        )
    )
    assert created.id is not None
    assert repo.get_server(created.id).webhook_payload_preset == WebhookPreset.sonarr_radarr


def test_create_server_defaults_preset_to_custom(repo: Repo) -> None:
    from mediascanmonitor.db.models import WebhookPreset
    from mediascanmonitor.db.schemas import ServerCreate as SC

    created = repo.create_server(SC(name="hook-default", type=ServerType.webhook))
    assert created.id is not None
    assert repo.get_server(created.id).webhook_payload_preset == WebhookPreset.custom


def test_update_server_changes_preset(repo: Repo) -> None:
    from mediascanmonitor.db.models import WebhookPreset
    from mediascanmonitor.db.schemas import ServerCreate as SC

    created = repo.create_server(SC(name="hook-upd", type=ServerType.webhook))
    assert created.id is not None
    repo.update_server(created.id, ServerUpdate(webhook_payload_preset=WebhookPreset.sonarr_radarr))
    assert repo.get_server(created.id).webhook_payload_preset == WebhookPreset.sonarr_radarr
