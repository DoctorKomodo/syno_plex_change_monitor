"""Tests for the SQLModel tables and enums (contract sections 1-2)."""

from sqlalchemy import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from mediascanmonitor.db.models import (
    DebounceMode,
    FileType,
    Folder,
    FsEventType,
    ScanMode,
    Server,
    ServerType,
    Setting,
    parse_event_types,
    serialize_event_types,
)


def _memory_engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_enum_values_match_contract() -> None:
    assert ServerType.webhook.value == "webhook"
    assert ServerType.plex.value == "plex"
    assert ServerType.emby.value == "emby"
    assert ServerType.jellyfin.value == "jellyfin"
    assert ServerType.audiobookshelf.value == "audiobookshelf"
    assert ScanMode.targeted.value == "targeted"
    assert ScanMode.library.value == "library"
    assert DebounceMode.off.value == "off"
    assert DebounceMode.trailing.value == "trailing"


def test_server_defaults() -> None:
    server = Server(name="plex1", type=ServerType.plex)
    assert server.base_url == ""
    assert server.verify_tls is True
    assert server.timeout_seconds == 10.0
    assert server.secret_encrypted is None
    assert server.scan_mode is ScanMode.targeted
    assert server.debounce_mode is DebounceMode.trailing
    assert server.debounce_window_seconds == 30
    assert server.retry_attempts == 3
    assert server.enabled is True


def test_relationships_persist() -> None:
    engine = _memory_engine()
    with Session(engine) as session:
        server = Server(name="plex1", type=ServerType.plex)
        folder = Folder(server=server, path="/data/tv", library_id="2")
        folder.filetypes.extend(
            [FileType(extension="mkv"), FileType(extension="srt")],
        )
        session.add(server)
        session.commit()
        session.refresh(server)
        assert server.id is not None
        assert len(server.folders) == 1
        assert server.folders[0].server_id == server.id
        assert {ft.extension for ft in server.folders[0].filetypes} == {"mkv", "srt"}


def test_cascade_delete_removes_folders_and_filetypes() -> None:
    engine = _memory_engine()
    with Session(engine) as session:
        server = Server(name="plex1", type=ServerType.plex)
        folder = Folder(server=server, path="/data/tv")
        folder.filetypes.append(FileType(extension="mkv"))
        session.add(server)
        session.commit()
        server_id = server.id

    with Session(engine) as session:
        server = session.get(Server, server_id)
        assert server is not None
        session.delete(server)
        session.commit()

    with Session(engine) as session:
        assert list(session.exec(select(Folder)).all()) == []
        assert list(session.exec(select(FileType)).all()) == []


def test_setting_table_round_trips() -> None:
    engine = _memory_engine()
    with Session(engine) as session:
        session.add(Setting(key="inotify_gate", value="enforce"))
        session.commit()
    with Session(engine) as session:
        row = session.get(Setting, "inotify_gate")
        assert row is not None
        assert row.value == "enforce"


def test_fs_event_type_values_and_order() -> None:
    assert FsEventType.created.value == "created"
    assert FsEventType.moved_to.value == "moved_to"
    assert FsEventType.deleted.value == "deleted"
    assert FsEventType.moved_from.value == "moved_from"
    assert list(FsEventType) == [
        FsEventType.created,
        FsEventType.moved_to,
        FsEventType.deleted,
        FsEventType.moved_from,
    ]


def test_serialize_event_types_joins_values_with_commas() -> None:
    assert serialize_event_types([FsEventType.deleted, FsEventType.created]) == "deleted,created"


def test_serialize_event_types_of_full_enum_matches_declaration_order() -> None:
    # This exact string is what migration 0004's server_default must equal (Task 2).
    assert serialize_event_types(FsEventType) == "created,moved_to,deleted,moved_from"


def test_parse_event_types_round_trips() -> None:
    assert parse_event_types("created,deleted") == frozenset(
        {FsEventType.created, FsEventType.deleted}
    )


def test_parse_event_types_empty_string_yields_empty_set() -> None:
    assert parse_event_types("") == frozenset()
