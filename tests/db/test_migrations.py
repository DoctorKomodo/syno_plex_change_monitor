"""The DB migrates to head and the folder table carries every expected column."""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import inspect

from mediascanmonitor.db.session import init_db


def test_folder_table_has_library_name_column(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "app.db")  # runs Alembic upgrade to head
    columns = {c["name"] for c in inspect(engine).get_columns("folder")}
    assert "library_name" in columns


def test_server_table_has_webhook_payload_preset_column(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "app.db")  # runs Alembic upgrade to head
    columns = {c["name"] for c in inspect(engine).get_columns("server")}
    assert "webhook_payload_preset" in columns


def test_webhook_payload_preset_server_default_is_custom(tmp_path: Path) -> None:
    # A row inserted WITHOUT the column (as a pre-0003 row reads after migration) takes the
    # server_default 'custom' — this is the "existing rows keep current behaviour" guarantee.
    engine = init_db(tmp_path / "app.db")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO server (name, type, base_url, verify_tls, timeout_seconds, "
                "scan_mode, debounce_mode, debounce_window_seconds, retry_attempts, enabled) "
                "VALUES ('h', 'webhook', '', 1, 10.0, 'targeted', 'trailing', 30, 3, 1)"
            )
        )
        value = conn.execute(
            sa.text("SELECT webhook_payload_preset FROM server WHERE name = 'h'")
        ).scalar_one()
    assert value == "custom"


def test_server_table_has_event_types_column(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "app.db")  # runs Alembic upgrade to head
    columns = {c["name"] for c in inspect(engine).get_columns("server")}
    assert "event_types" in columns


def test_event_types_server_default_is_all_four(tmp_path: Path) -> None:
    # A row inserted WITHOUT the column (as a pre-0004 row reads after migration) takes the
    # server_default — every existing server keeps receiving every event type.
    engine = init_db(tmp_path / "app.db")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO server (name, type, base_url, verify_tls, timeout_seconds, "
                "scan_mode, debounce_mode, debounce_window_seconds, retry_attempts, enabled) "
                "VALUES ('h', 'webhook', '', 1, 10.0, 'targeted', 'trailing', 30, 3, 1)"
            )
        )
        value = conn.execute(
            sa.text("SELECT event_types FROM server WHERE name = 'h'")
        ).scalar_one()
    assert value == "created,moved_to,deleted,moved_from"
