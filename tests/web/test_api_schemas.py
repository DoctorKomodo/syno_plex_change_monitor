"""Redacted read-models + per-type specs (contract §D)."""

from mediascanmonitor.db.models import ScanMode, ServerType
from mediascanmonitor.db.repo import Repo
from mediascanmonitor.db.schemas import FolderCreate, ServerCreate
from mediascanmonitor.web.api_schemas import (
    SERVER_TYPE_SPECS,
    FolderRead,
    ServerRead,
)


def _seed(repo: Repo) -> tuple[int, int]:
    server = repo.create_server(
        ServerCreate(name="plex", type=ServerType.plex, base_url="http://p:32400", secret="tok")
    )
    assert server.id is not None
    folder = repo.create_folder(
        server.id, FolderCreate(path="/data/tv", library_id="2", extensions=["mkv", "mp4"])
    )
    assert folder.id is not None
    return server.id, folder.id


def test_folder_read_sorts_extensions(repo: Repo) -> None:
    server = repo.create_server(ServerCreate(name="p", type=ServerType.plex, secret="t"))
    assert server.id is not None
    folder = repo.create_folder(
        server.id, FolderCreate(path="/data/tv", extensions=["mp4", "avi", "mkv"])
    )
    read = FolderRead.from_model(repo.get_folder(folder.id))  # type: ignore[arg-type]
    assert read.extensions == ["avi", "mkv", "mp4"]


def test_server_read_redacts_secret(repo: Repo) -> None:
    server_id, _ = _seed(repo)
    server = repo.get_server(server_id)
    assert server is not None
    read = ServerRead.from_model(server, repo.list_folders(server_id))
    dumped = read.model_dump()
    assert "secret" not in dumped
    assert "secret_encrypted" not in dumped
    assert read.has_secret is True
    assert "tok" not in str(dumped)
    assert server.secret_encrypted is not None
    assert server.secret_encrypted not in str(dumped)


def test_server_read_has_secret_false_when_unset(repo: Repo) -> None:
    server = repo.create_server(ServerCreate(name="hook", type=ServerType.webhook))
    assert server.id is not None
    read = ServerRead.from_model(server, [])
    assert read.has_secret is False


def test_server_read_supported_scan_modes_from_registry(repo: Repo) -> None:
    server = repo.create_server(ServerCreate(name="emby", type=ServerType.emby, secret="t"))
    assert server.id is not None
    read = ServerRead.from_model(server, [])
    # emby only supports library mode (see tests/servers/test_emby.py).
    assert read.supported_scan_modes == [ScanMode.library]


def test_server_read_carries_event_types_in_declaration_order(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType
    from mediascanmonitor.db.schemas import ServerCreate

    server = repo.create_server(
        ServerCreate(
            name="narrow-events",
            type=ServerType.plex,
            base_url="https://plex:32400",
            secret="tok",
            # deliberately out of declaration order, to prove from_model re-sorts
            event_types=[FsEventType.deleted, FsEventType.created],
        )
    )
    read = ServerRead.from_model(server, [])
    assert read.event_types == [FsEventType.created, FsEventType.deleted]


def test_server_read_includes_folders(repo: Repo) -> None:
    server_id, folder_id = _seed(repo)
    server = repo.get_server(server_id)
    assert server is not None
    read = ServerRead.from_model(server, repo.list_folders(server_id))
    assert [f.id for f in read.folders] == [folder_id]


def test_server_type_specs_cover_every_type() -> None:
    assert set(SERVER_TYPE_SPECS) == set(ServerType)
    assert SERVER_TYPE_SPECS[ServerType.webhook].requires_secret is False
    assert SERVER_TYPE_SPECS[ServerType.webhook].is_webhook is True
    # The webhook adapter sends to base_url (the target URL), so the UI must expose it.
    assert SERVER_TYPE_SPECS[ServerType.webhook].requires_base_url is True
    assert SERVER_TYPE_SPECS[ServerType.plex].requires_secret is True
    assert SERVER_TYPE_SPECS[ServerType.plex].requires_base_url is True
