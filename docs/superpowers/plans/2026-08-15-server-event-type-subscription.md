# Per-server event-type subscription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each `Server` subscribe to a subset of the four filesystem event types (`created`, `moved_to`, `deleted`, `moved_from`), filtered at the routing layer, defaulting to all four (no behavior change until an operator opts out).

**Architecture:** `FsEventType` moves from `pipeline/events.py` to `db/models.py` (alongside `ScanMode`/`DebounceMode`) so `Server` can persist a comma-separated subset of it. The set flows `Server` → (repo) → `ServerCreate`/`ServerUpdate`/`ServerRead` → (`config/runtime.py`) → `FolderRoute.event_types` → `pipeline/router.py`'s `route()`, which drops non-subscribed events before a `ScanRequest` is ever built — one more skip condition next to the existing extension-match check. The web form gets a checkbox group per server (Delivery fieldset), all checked by default.

**Tech Stack:** Python 3.14, SQLModel, Alembic, Pydantic v2, FastAPI/Starlette, Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-08-15-server-event-type-subscription-design.md` — read it for the full rationale; this plan implements it task-by-task.

## Global Constraints

- No `from __future__ import annotations` anywhere; leave forward references unquoted (PEP 649).
- Enums subclass `StrEnum`, never `(str, Enum)`.
- `ruff` `select = E, F, I, UP, B, C4, SIM, RUF` (per-file-ignore: `B` under `tests/**`); imports
  separated stdlib / third-party / first-party (`mediascanmonitor`) by a blank line. Run
  `ruff format` then `ruff check --fix` on touched files before treating a step as done.
- `mypy --strict mediascanmonitor` must stay clean.
- Every DB schema change ships an Alembic migration — never silently break an existing `app.db`.
- Full gate before each commit: `ruff check .`, `ruff format --check .`, `mypy mediascanmonitor`,
  `pytest`.

---

### Task 1: Relocate `FsEventType` to `db/models.py` + parse/serialize helpers

**Files:**
- Modify: `mediascanmonitor/db/models.py`
- Modify: `mediascanmonitor/pipeline/events.py`
- Test: `tests/db/test_models.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `mediascanmonitor.db.models.FsEventType` (StrEnum: `created`, `moved_to`, `deleted`,
  `moved_from`, in that declaration order); `mediascanmonitor.db.models.serialize_event_types(types:
  Iterable[FsEventType]) -> str`; `mediascanmonitor.db.models.parse_event_types(raw: str) ->
  frozenset[FsEventType]`. `mediascanmonitor.pipeline.events.FsEventType` keeps working as a
  re-export (same object), so `watcher/inotify_backend.py` and `servers/webhook.py`, which both
  import `FsEventType` from `pipeline.events`, need no changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_models.py` (add the import to the existing `from
mediascanmonitor.db.models import (...)` block at the top of the file, alongside `DebounceMode`,
`FileType`, etc.):

```python
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
```

New tests, appended at the end of the file:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/db/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'FsEventType' from 'mediascanmonitor.db.models'`

- [ ] **Step 3: Implement in `db/models.py`**

Add the `Iterable` import and the new enum + helpers. The enum goes beside the other enums (after
`WebhookPreset`, before `Server`); the helpers go right after it, since `Server.event_types`
(Task 2) needs `serialize_event_types` as its default-value expression.

Modify the import block at the top of `mediascanmonitor/db/models.py`:

```python
from collections.abc import Iterable
from enum import StrEnum

from sqlmodel import Field, Relationship, SQLModel
```

Insert after the `WebhookPreset` class (`mediascanmonitor/db/models.py:37-39`), before
`class Server(SQLModel, table=True):`:

```python
class FsEventType(StrEnum):
    created = "created"  # inotify CREATE
    moved_to = "moved_to"  # inotify MOVED_TO
    deleted = "deleted"  # inotify DELETE
    moved_from = "moved_from"  # inotify MOVED_FROM


def serialize_event_types(types: Iterable[FsEventType]) -> str:
    """Join event types into the comma-separated form ``Server.event_types`` stores."""
    return ",".join(t.value for t in types)


def parse_event_types(raw: str) -> frozenset[FsEventType]:
    """Inverse of ``serialize_event_types``. An empty string parses to an empty set."""
    return frozenset(FsEventType(v) for v in raw.split(",") if v)
```

- [ ] **Step 4: Move the import in `pipeline/events.py`**

Replace the top of `mediascanmonitor/pipeline/events.py` (lines 8-18):

```python
from dataclasses import dataclass
from enum import StrEnum

from mediascanmonitor.db.models import ScanMode


class FsEventType(StrEnum):
    created = "created"  # inotify CREATE
    moved_to = "moved_to"  # inotify MOVED_TO
    deleted = "deleted"  # inotify DELETE
    moved_from = "moved_from"  # inotify MOVED_FROM
```

with:

```python
from dataclasses import dataclass

from mediascanmonitor.db.models import FsEventType, ScanMode
```

(`FsEventType` is used directly below in `FsEvent.event_type` and `ScanRequest.event_type`'s
annotations, so this import is not flagged as unused by ruff `F401` — no `# noqa` needed.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/db/test_models.py -v`
Expected: PASS (all 5 new tests + existing ones)

- [ ] **Step 6: Regression-check every existing importer of `FsEventType`**

Run: `pytest tests/pipeline/test_events.py tests/pipeline/test_router.py tests/watcher tests/servers -v`
Expected: PASS, unchanged — this proves the re-export covers `watcher/inotify_backend.py`,
`servers/webhook.py`, and every test file that does
`from mediascanmonitor.pipeline.events import FsEventType`.

- [ ] **Step 7: Lint and type-check**

Run: `ruff format mediascanmonitor/db/models.py mediascanmonitor/pipeline/events.py && ruff check --fix mediascanmonitor/db/models.py mediascanmonitor/pipeline/events.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add mediascanmonitor/db/models.py mediascanmonitor/pipeline/events.py tests/db/test_models.py
git commit -m "refactor(db): relocate FsEventType to db.models, add serialize/parse helpers"
```

---

### Task 2: `Server.event_types` column + migration `0004`

**Files:**
- Modify: `mediascanmonitor/db/models.py`
- Create: `mediascanmonitor/migrations/versions/0004_server_event_types.py`
- Test: `tests/db/test_migrations.py`
- Test: `tests/db/test_models.py`

**Interfaces:**
- Consumes: `FsEventType`, `serialize_event_types` from Task 1.
- Produces: `Server.event_types: str`, defaulting to `"created,moved_to,deleted,moved_from"`
  both at the Python level (fresh `SQLModel.metadata.create_all()`, e.g. `test_models.py`'s
  `_memory_engine()`) and at the DB level (Alembic `server_default`, for rows that predate this
  migration).

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_migrations.py` (it already imports `Path`, `sa`, `inspect`, `init_db` —
no new imports needed):

```python
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
```

Add one line to `tests/db/test_models.py::test_server_defaults` (`tests/db/test_models.py:40-50`),
after the existing `assert server.enabled is True`:

```python
    assert server.event_types == "created,moved_to,deleted,moved_from"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/db/test_migrations.py tests/db/test_models.py::test_server_defaults -v`
Expected: FAIL — `sqlalchemy.exc.OperationalError: no such column: event_types` (migration test)
and `AttributeError: 'Server' object has no attribute 'event_types'` (model test)

- [ ] **Step 3: Add the column to the `Server` model**

In `mediascanmonitor/db/models.py`, insert into `class Server(SQLModel, table=True):`
(`mediascanmonitor/db/models.py:42-63`), right after `webhook_payload_preset: WebhookPreset =
WebhookPreset.custom` and before the `folders: list[Folder] = Relationship(...)` line:

```python
    event_types: str = serialize_event_types(FsEventType)  # comma-separated FsEventType values
```

- [ ] **Step 4: Write the migration**

Create `mediascanmonitor/migrations/versions/0004_server_event_types.py`:

```python
"""server.event_types

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_types",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="created,moved_to,deleted,moved_from",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.drop_column("event_types")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/db/test_migrations.py tests/db/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run: `ruff format mediascanmonitor/db/models.py mediascanmonitor/migrations/versions/0004_server_event_types.py && ruff check --fix mediascanmonitor/db/models.py mediascanmonitor/migrations/versions/0004_server_event_types.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add mediascanmonitor/db/models.py mediascanmonitor/migrations/versions/0004_server_event_types.py tests/db/test_migrations.py tests/db/test_models.py
git commit -m "feat(db): add Server.event_types column + migration 0004"
```

---

### Task 3: `ServerCreate` / `ServerUpdate` schema fields

**Files:**
- Modify: `mediascanmonitor/db/schemas.py`
- Test: `tests/db/test_schemas.py`

**Interfaces:**
- Consumes: `FsEventType` (Task 1).
- Produces: `ServerCreate.event_types: list[FsEventType]` (default: all four, in declaration
  order); `ServerUpdate.event_types: list[FsEventType] | None` (default `None`, tri-state like
  every other `ServerUpdate` field).

- [ ] **Step 1: Write the failing tests**

Add to `tests/db/test_schemas.py`, after the `from mediascanmonitor.db.models import ...` line,
change it to also import `FsEventType`:

```python
from mediascanmonitor.db.models import DebounceMode, FsEventType, ScanMode, ServerType
```

Add one line to `test_server_create_defaults` (`tests/db/test_schemas.py:10-20`), after `assert
s.enabled is True`:

```python
    assert s.event_types == list(FsEventType)
```

New tests, appended after `test_server_update_tracks_only_set_fields`:

```python
def test_server_create_narrows_event_types() -> None:
    s = ServerCreate(
        name="hook",
        type=ServerType.webhook,
        event_types=[FsEventType.created, FsEventType.moved_to],
    )
    assert s.event_types == [FsEventType.created, FsEventType.moved_to]


def test_server_update_event_types_defaults_to_none() -> None:
    u = ServerUpdate()
    assert u.event_types is None
    assert "event_types" not in u.model_dump(exclude_unset=True)


def test_server_update_event_types_tracked_when_set() -> None:
    u = ServerUpdate(event_types=[FsEventType.deleted])
    assert u.model_dump(exclude_unset=True) == {"event_types": [FsEventType.deleted]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/db/test_schemas.py -v`
Expected: FAIL — `pydantic.error_wrappers.ValidationError` / `AttributeError: 'ServerCreate'
object has no attribute 'event_types'`

- [ ] **Step 3: Implement**

In `mediascanmonitor/db/schemas.py`, change the import line (`mediascanmonitor/db/schemas.py:12`):

```python
from mediascanmonitor.db.models import DebounceMode, FsEventType, ScanMode, ServerType, WebhookPreset
```

Add to `ServerCreate` (`mediascanmonitor/db/schemas.py:16-33`), after `webhook_payload_preset:
WebhookPreset = WebhookPreset.custom`:

```python
    event_types: list[FsEventType] = Field(default_factory=lambda: list(FsEventType))
```

Add to `ServerUpdate` (`mediascanmonitor/db/schemas.py:36-54`), after `webhook_payload_preset:
WebhookPreset | None = None`:

```python
    event_types: list[FsEventType] | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/db/test_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `ruff format mediascanmonitor/db/schemas.py && ruff check --fix mediascanmonitor/db/schemas.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add mediascanmonitor/db/schemas.py tests/db/test_schemas.py
git commit -m "feat(db): add event_types to ServerCreate/ServerUpdate schemas"
```

---

### Task 4: Repo layer — persist and update `event_types`

**Files:**
- Modify: `mediascanmonitor/db/repo.py`
- Test: `tests/db/test_repo.py`

**Interfaces:**
- Consumes: `serialize_event_types` (Task 1), `ServerCreate.event_types` /
  `ServerUpdate.event_types` (Task 3).
- Produces: `Repo.create_server`, `Repo.create_server_with_folders`, `Repo.update_server`,
  `Repo.update_server_with_folders` all read/write `Server.event_types` correctly, including the
  explicit-`None`-is-a-no-op tri-state on update (mirroring how `update_folder` already handles
  `extensions`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/db/test_repo.py`, after `test_create_server_with_folders_persists_both`
(`tests/db/test_repo.py:40-52`):

```python
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
```

Add to `tests/db/test_repo.py`, after `test_update_server_clears_secret_when_explicitly_none`
(`tests/db/test_repo.py:141-148`):

```python
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
```

Add to `tests/db/test_repo.py`, after
`test_update_server_with_folders_changes_fields_and_swaps_folders`
(`tests/db/test_repo.py:55-73` — check the exact end line before inserting):

```python
def test_update_server_with_folders_narrows_event_types(repo: Repo) -> None:
    from mediascanmonitor.db.models import FsEventType

    server = repo.create_server(make_server(name="combo"))
    assert server.id is not None
    updated = repo.update_server_with_folders(
        server.id, ServerUpdate(event_types=[FsEventType.moved_from]), []
    )
    assert updated.event_types == "moved_from"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/db/test_repo.py -v`
Expected: FAIL — created/updated servers all have `event_types == "created,moved_to,deleted,moved_from"`
regardless of what was passed (the field isn't wired into `Server(...)` construction or the
`update_server` field loop yet), so every narrowing/clearing assertion fails.

- [ ] **Step 3: Implement**

In `mediascanmonitor/db/repo.py`, change the import (`mediascanmonitor/db/repo.py:21`):

```python
from mediascanmonitor.db.models import FileType, Folder, Server, Setting, serialize_event_types
```

In `create_server` (`mediascanmonitor/db/repo.py:54-77`), add to the `Server(...)` call, after
`webhook_payload_preset=data.webhook_payload_preset,`:

```python
                event_types=serialize_event_types(data.event_types),
```

In `create_server_with_folders` (`mediascanmonitor/db/repo.py:93-128`), add the identical line to
its `Server(...)` call, after `webhook_payload_preset=server_data.webhook_payload_preset,`:

```python
                event_types=serialize_event_types(server_data.event_types),
```

In `update_server` (`mediascanmonitor/db/repo.py:130-142`), replace:

```python
    def update_server(self, server_id: int, data: ServerUpdate) -> Server:
        with self._session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:
                raise KeyError(f"server {server_id} not found")
            fields = data.model_dump(exclude_unset=True)
            if "secret" in fields:
                secret = fields.pop("secret")
                server.secret_encrypted = self._box.encrypt(secret) if secret is not None else None
            for key, value in fields.items():
                setattr(server, key, value)
            session.commit()  # server is session-tracked from get(); no add() needed
            return server
```

with:

```python
    def update_server(self, server_id: int, data: ServerUpdate) -> Server:
        with self._session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:
                raise KeyError(f"server {server_id} not found")
            fields = data.model_dump(exclude_unset=True)
            if "secret" in fields:
                secret = fields.pop("secret")
                server.secret_encrypted = self._box.encrypt(secret) if secret is not None else None
            # Mirrors update_folder's "extensions" handling: explicit None is a no-op, same as
            # omitted — event_types has no "clear via null" semantic ([] already means "none").
            new_event_types = fields.pop("event_types", None)
            for key, value in fields.items():
                setattr(server, key, value)
            if new_event_types is not None:
                server.event_types = serialize_event_types(new_event_types)
            session.commit()  # server is session-tracked from get(); no add() needed
            return server
```

Apply the identical `new_event_types` change to `update_server_with_folders`
(`mediascanmonitor/db/repo.py:144-166`):

```python
    def update_server_with_folders(
        self, server_id: int, data: ServerUpdate, folders: list[FolderCreate]
    ) -> Server:
        """Update a server's fields AND replace its whole folder set in ONE transaction.

        Combines update_server's field/secret tri-state with _set_server_folders so the detail
        page's single "Save changes" persists both atomically (all-or-nothing) and the caller
        rebuilds once. An empty ``folders`` clears them. Raises KeyError if the server is gone.
        Mirror of create_server_with_folders.
        """
        with self._session_factory() as session:
            server = session.get(Server, server_id)
            if server is None:
                raise KeyError(f"server {server_id} not found")
            fields = data.model_dump(exclude_unset=True)
            if "secret" in fields:
                secret = fields.pop("secret")
                server.secret_encrypted = self._box.encrypt(secret) if secret is not None else None
            new_event_types = fields.pop("event_types", None)
            for key, value in fields.items():
                setattr(server, key, value)
            if new_event_types is not None:
                server.event_types = serialize_event_types(new_event_types)
            _set_server_folders(server, folders)
            session.commit()  # server is session-tracked from get(); no add() needed
            return server
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/db/test_repo.py -v`
Expected: PASS

- [ ] **Step 5: Run the full DB test suite (regression check)**

Run: `pytest tests/db -v`
Expected: PASS — confirms `update_server`'s existing secret/field tests still pass with the new
`new_event_types` pop inserted into the field loop.

- [ ] **Step 6: Lint and type-check**

Run: `ruff format mediascanmonitor/db/repo.py && ruff check --fix mediascanmonitor/db/repo.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add mediascanmonitor/db/repo.py tests/db/test_repo.py
git commit -m "feat(db): thread event_types through Repo create/update methods"
```

---

### Task 5: `ServerRead.event_types`

**Files:**
- Modify: `mediascanmonitor/web/api_schemas.py`
- Test: `tests/web/test_api_schemas.py`

**Interfaces:**
- Consumes: `FsEventType`, `parse_event_types` (Task 1).
- Produces: `ServerRead.event_types: list[FsEventType]`, in enum-declaration order, populated by
  `ServerRead.from_model`.

- [ ] **Step 1: Write the failing test**

Add to `tests/web/test_api_schemas.py`, near `test_server_read_supported_scan_modes_from_registry`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/web/test_api_schemas.py::test_server_read_carries_event_types_in_declaration_order -v`
Expected: FAIL — `TypeError: ServerRead() got an unexpected keyword argument` or `AttributeError:
'ServerRead' object has no attribute 'event_types'`

- [ ] **Step 3: Implement**

In `mediascanmonitor/web/api_schemas.py`, change the import (`mediascanmonitor/web/api_schemas.py:15-22`):

```python
from mediascanmonitor.db.models import (
    DebounceMode,
    Folder,
    FsEventType,
    ScanMode,
    Server,
    ServerType,
    WebhookPreset,
    parse_event_types,
)
```

Add to `ServerRead` (`mediascanmonitor/web/api_schemas.py:50-68`), after `webhook_payload_preset:
WebhookPreset`:

```python
    event_types: list[FsEventType]
```

Add to `ServerRead.from_model` (`mediascanmonitor/web/api_schemas.py:70-92`), after
`webhook_payload_preset=server.webhook_payload_preset,`:

```python
            event_types=[t for t in FsEventType if t in parse_event_types(server.event_types)],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/web/test_api_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Lint and type-check**

Run: `ruff format mediascanmonitor/web/api_schemas.py && ruff check --fix mediascanmonitor/web/api_schemas.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add mediascanmonitor/web/api_schemas.py tests/web/test_api_schemas.py
git commit -m "feat(web): expose event_types on ServerRead"
```

---

### Task 6: Routing — `FolderRoute.event_types` + the `route()` filter

**Files:**
- Modify: `mediascanmonitor/config/runtime.py`
- Modify: `mediascanmonitor/pipeline/router.py`
- Modify: `tests/config/test_runtime.py`
- Modify: `tests/pipeline/factories.py`
- Test: `tests/pipeline/test_router.py`

**Interfaces:**
- Consumes: `FsEventType`, `parse_event_types` (Task 1).
- Produces: `FolderRoute.event_types: frozenset[FsEventType]`; `route()` drops any event whose
  type isn't in the matching route's `event_types` before building a `ScanRequest` — no debounce
  entry, no dispatch, no live-feed entry for a filtered-out event.

This is the task that makes the feature actually work at runtime; everything before it was
plumbing, everything after it is the UI.

- [ ] **Step 1: Write the failing tests**

`FolderRoute` is a plain frozen dataclass with no field defaults, so adding a required field
breaks every direct construction site. Fix the two test helpers FIRST (this is scaffolding for
the task's real tests, not a separate task — right-sized: the router filter is untestable without
a `FolderRoute` that can carry `event_types`).

In `tests/pipeline/factories.py`, add the import and the new kwarg to `make_folder_route`
(`tests/pipeline/factories.py:1-50`):

```python
from mediascanmonitor.db.models import DebounceMode, FsEventType, ScanMode, ServerType, WebhookPreset
```

```python
def make_folder_route(
    *,
    server_id: int = 1,
    server_name: str = "plex-1",
    path: str = "/data/tv",
    extensions: frozenset[str] = frozenset({"mkv"}),
    library_id: str | None = "2",
    scan_mode: ScanMode = ScanMode.targeted,
    event_types: frozenset[FsEventType] = frozenset(FsEventType),
) -> FolderRoute:
    return FolderRoute(
        server_id=server_id,
        server_name=server_name,
        path=path,
        extensions=extensions,
        library_id=library_id,
        scan_mode=scan_mode,
        event_types=event_types,
    )
```

In `tests/config/test_runtime.py`, update `test_folder_route_fields_frozen_slotted`
(`tests/config/test_runtime.py:77-90`):

```python
def test_folder_route_fields_frozen_slotted() -> None:
    fr = FolderRoute(
        server_id=1,
        server_name="plex-main",
        path="/data/media/tv",
        extensions=frozenset({"mkv", "srt"}),
        library_id="2",
        scan_mode=ScanMode.targeted,
        event_types=frozenset(FsEventType),
    )
    assert fr.path == "/data/media/tv"
    assert fr.extensions == frozenset({"mkv", "srt"})
    assert fr.event_types == frozenset(FsEventType)
    assert not hasattr(fr, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fr.path = "/elsewhere"  # type: ignore[misc]
```

Add `FsEventType` to the `db.models` import in `tests/config/test_runtime.py`
(`tests/config/test_runtime.py:15-23`):

```python
from mediascanmonitor.db.models import (
    DebounceMode,
    FileType,
    Folder,
    FsEventType,
    ScanMode,
    Server,
    ServerType,
    WebhookPreset,
)
```

Add `event_types` to `make_server`'s kwargs (`tests/config/test_runtime.py:158-183`), so a test
can build a server with a non-default subscription:

```python
def make_server(
    server_id: int,
    *,
    name: str,
    type: ServerType = ServerType.plex,
    base_url: str = "https://plex.local:32400",
    scan_mode: ScanMode = ScanMode.targeted,
    debounce_mode: DebounceMode = DebounceMode.trailing,
    enabled: bool = True,
    webhook_payload_preset: WebhookPreset = WebhookPreset.custom,
    event_types: str = "created,moved_to,deleted,moved_from",
) -> Server:
    return Server(
        id=server_id,
        name=name,
        type=type,
        base_url=base_url,
        verify_tls=True,
        timeout_seconds=10.0,
        secret_encrypted="ciphertext-ignored-by-stub",
        scan_mode=scan_mode,
        debounce_mode=debounce_mode,
        debounce_window_seconds=30,
        retry_attempts=3,
        enabled=enabled,
        webhook_payload_preset=webhook_payload_preset,
        event_types=event_types,
    )
```

Extend `test_build_runtime_config_happy_path` (`tests/config/test_runtime.py:206-243`) — add
after `assert route.scan_mode is ScanMode.targeted`:

```python
    assert route.event_types == frozenset(FsEventType)
```

New test, appended after `test_build_runtime_config_carries_webhook_payload_preset`:

```python
def test_build_runtime_config_carries_narrowed_event_types() -> None:
    server = make_server(1, name="hook", event_types="created,deleted")
    folder = make_folder(10, server_id=1, path="/data/tv", library_id="2", extensions=["mkv"])
    repo = FakeRepo(servers=[server], folders_by_server={1: [folder]}, secrets={1: None})

    cfg = build_runtime_config(cast("Repo", repo))

    assert cfg.routes[0].event_types == frozenset({FsEventType.created, FsEventType.deleted})
```

Add `FsEventType` import to `tests/pipeline/test_router.py` (it already imports `FsEventType`
from `pipeline.events` at line 2 — no change needed there). New tests, appended at the end of
the file:

```python
def test_route_excludes_server_not_subscribed_to_event_type() -> None:
    route_created_only = make_folder_route(
        server_id=1, server_name="hook", event_types=frozenset({FsEventType.created})
    )
    config = make_runtime_config([route_created_only])

    deleted_event = FsEvent(path="/data/tv/ep.mkv", event_type=FsEventType.deleted, is_dir=False)
    assert route(deleted_event, config) == []


def test_route_includes_server_subscribed_to_event_type() -> None:
    route_created_only = make_folder_route(
        server_id=1, server_name="hook", event_types=frozenset({FsEventType.created})
    )
    config = make_runtime_config([route_created_only])

    reqs = route(_event("/data/tv/ep.mkv"), config)  # _event() defaults to FsEventType.created
    assert {r.server_id for r in reqs} == {1}


def test_route_empty_event_types_matches_nothing() -> None:
    route_none = make_folder_route(server_id=1, server_name="hook", event_types=frozenset())
    config = make_runtime_config([route_none])
    assert route(_event("/data/tv/ep.mkv"), config) == []
```

`tests/pipeline/test_router.py` needs `FsEvent` imported for the first new test — check its
existing import line (`from mediascanmonitor.pipeline.events import FsEvent, FsEventType`); it
already imports both, so no change needed there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/config/test_runtime.py tests/pipeline/test_router.py tests/pipeline/factories.py -v`
Expected: FAIL — `TypeError: FolderRoute.__init__() missing 1 required keyword-only argument:
'event_types'` (from every existing direct `FolderRoute(...)` construction and from
`make_folder_route`), and `TypeError: Server.__init__() got an unexpected keyword argument
'event_types'` is NOT expected here since `Server.event_types` already has a default from Task 2 —
the new-behavior tests (`test_route_excludes_...` etc.) fail instead with `AttributeError:
'FolderRoute' object has no attribute 'event_types'`.

- [ ] **Step 3: Implement in `config/runtime.py`**

Change the import (`mediascanmonitor/config/runtime.py:11`):

```python
from mediascanmonitor.db.models import (
    DebounceMode,
    FsEventType,
    ScanMode,
    ServerType,
    WebhookPreset,
    parse_event_types,
)
```

Add to `FolderRoute` (`mediascanmonitor/config/runtime.py:37-45`), after `scan_mode: ScanMode`:

```python
    event_types: frozenset[FsEventType]
```

Add to the `FolderRoute(...)` construction inside `build_runtime_config`
(`mediascanmonitor/config/runtime.py:87-98`), after `scan_mode=server.scan_mode,`:

```python
                    event_types=parse_event_types(server.event_types),
```

- [ ] **Step 4: Implement in `pipeline/router.py`**

Change the docstring and add the skip condition in `route()`
(`mediascanmonitor/pipeline/router.py:31-48`):

```python
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
```

(The rest of the function — `if folder_route.scan_mode is ScanMode.targeted: ...` through the
`return requests` — is unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/config/test_runtime.py tests/pipeline/test_router.py tests/pipeline -v`
Expected: PASS

- [ ] **Step 6: Run the full pipeline + config + engine suite (regression check)**

Run: `pytest tests/pipeline tests/config tests/test_engine.py -v` (adjust the engine test path if
it differs — locate it with `find tests -iname "test_engine*"` if unsure)
Expected: PASS — confirms `route()`'s new skip condition doesn't change behavior for any existing
test, all of which build routes with the (new) default `event_types=frozenset(FsEventType)`.

- [ ] **Step 7: Lint and type-check**

Run: `ruff format mediascanmonitor/config/runtime.py mediascanmonitor/pipeline/router.py tests/config/test_runtime.py tests/pipeline/factories.py tests/pipeline/test_router.py && ruff check --fix mediascanmonitor/config/runtime.py mediascanmonitor/pipeline/router.py tests/config/test_runtime.py tests/pipeline/factories.py tests/pipeline/test_router.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add mediascanmonitor/config/runtime.py mediascanmonitor/pipeline/router.py tests/config/test_runtime.py tests/pipeline/factories.py tests/pipeline/test_router.py
git commit -m "feat(pipeline): filter routing by per-server event-type subscription"
```

---

### Task 7: Web form parsing — `ui_create_server_with_folders` / `ui_update_server`

**Files:**
- Modify: `mediascanmonitor/web/pages.py`
- Test: `tests/web/test_ui_forms.py`

**Interfaces:**
- Consumes: `FsEventType` (Task 1), `ServerCreate.event_types` / `ServerUpdate` field dict (Task
  3), `Server.event_types` (Task 2, for assertions).
- Produces: both `/ui/servers/new` (POST) and `/ui/servers/{id}/update` (POST) read
  `form.getlist("event_types")` and persist the selected subset. This task is independently
  testable via HTTP POST, without touching the GET pages or templates (Task 8).

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_ui_forms.py`, after `test_ui_create_server_with_folders_redirects_and_persists`:

```python
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
```

Add to `tests/web/test_ui_forms.py`, after `test_ui_update_saves_fields_and_folders_together`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/web/test_ui_forms.py -v`
Expected: FAIL — every new assertion sees `event_types == "created,moved_to,deleted,moved_from"`
(the schema default) instead of the narrowed/emptied value, because the form handlers don't read
`event_types` from the posted form yet.

- [ ] **Step 3: Implement**

In `mediascanmonitor/web/pages.py`, change the import (`mediascanmonitor/web/pages.py:33`):

```python
from mediascanmonitor.db.models import DebounceMode, FsEventType, ScanMode, ServerType, WebhookPreset
```

In `ui_create_server_with_folders` (`mediascanmonitor/web/pages.py:369-422`), add to the
`ServerCreate(...)` call, after `webhook_payload_preset=webhook_payload_preset,`:

```python
            event_types=[FsEventType(v) for v in form.getlist("event_types")],
```

In `ui_update_server` (`mediascanmonitor/web/pages.py:544-603`), add to the `fields: dict[str,
Any]` literal, after `"webhook_payload_preset": webhook_payload_preset,`:

```python
            "event_types": [FsEventType(v) for v in form.getlist("event_types")],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/web/test_ui_forms.py -v`
Expected: PASS

- [ ] **Step 5: Run the full web test suite (regression check)**

Run: `pytest tests/web -v`
Expected: PASS — every pre-existing form test that doesn't post `event_types` now persists an
empty `event_types` on that particular server (since the form always fully replaces it), but none
of those pre-existing tests assert on `event_types`, so nothing breaks. This is expected,
deliberate behavior (see spec §Web UI), not a regression — the "full replace" semantics already
apply to `folders` on this same combined-save form (`test_ui_update_empty_folder_rows_clears_all`).

- [ ] **Step 6: Lint and type-check**

Run: `ruff format mediascanmonitor/web/pages.py && ruff check --fix mediascanmonitor/web/pages.py && mypy mediascanmonitor`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add mediascanmonitor/web/pages.py tests/web/test_ui_forms.py
git commit -m "feat(web): parse event_types checkbox group in create/update form handlers"
```

---

### Task 8: Web checkbox rendering — GET pages + `_server_form_fields.html`

**Files:**
- Modify: `mediascanmonitor/web/pages.py`
- Modify: `mediascanmonitor/web/templates/_server_form_fields.html`
- Test: `tests/web/test_pages.py`

**Interfaces:**
- Consumes: `FsEventType` (Task 1), `ServerRead.event_types` (Task 5).
- Produces: `GET /servers/new` renders four checkboxes, all checked; `GET /servers/{id}` renders
  four checkboxes, checked to match the saved server's `event_types`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_pages.py`, near `test_webhook_form_renders_payload_preset_select`:

```python
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
```

(`ServerCreate` and `ServerType` are already imported at the top of `tests/web/test_pages.py` —
check the file's existing import block; every other test in the file uses them unqualified.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/web/test_pages.py::test_new_server_form_renders_all_four_event_type_checkboxes_checked tests/web/test_pages.py::test_server_detail_preselects_saved_event_types -v`
Expected: FAIL — `assert re.search(...)` finds nothing, since no `event_types` checkboxes exist
in the rendered HTML yet.

- [ ] **Step 3: Implement in `pages.py`**

Add a module-level label map, near `_webhook_preset_options` (`mediascanmonitor/web/pages.py:105-109`):

```python
_EVENT_TYPE_LABELS: dict[FsEventType, str] = {
    FsEventType.created: "Created",
    FsEventType.moved_to: "Moved in",
    FsEventType.deleted: "Deleted",
    FsEventType.moved_from: "Moved out",
}
```

In `server_new_page` (`mediascanmonitor/web/pages.py:165-190`), add to the context dict, after
`"debounce_modes": [m.value for m in DebounceMode],`:

```python
            "event_type_options": list(_EVENT_TYPE_LABELS.items()),
```

In `server_detail` (`mediascanmonitor/web/pages.py:207-227`), add to the context dict, after
`"debounce_modes": [m.value for m in DebounceMode],`:

```python
            "event_type_options": list(_EVENT_TYPE_LABELS.items()),
```

- [ ] **Step 4: Implement in `_server_form_fields.html`**

In `mediascanmonitor/web/templates/_server_form_fields.html`, add to the "Delivery" fieldset
(`mediascanmonitor/web/templates/_server_form_fields.html:112-143`), after the "Debounce window"
`<label class="field">...</label>` block, still inside `<div class="form-grid">`:

```html
    <div class="field field-event-types">
      <span class="field-label">Event types</span>
      <div class="toggles">
        {% for value, label in event_type_options %}
        <label class="toggle">
          <input type="checkbox" name="event_types" value="{{ value.value }}"
            {% if creating or value in server.event_types %}checked{% endif %}>
          {{ label }}
        </label>
        {% endfor %}
      </div>
    </div>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/web/test_pages.py -v`
Expected: PASS

- [ ] **Step 6: Run the full web test suite (regression check)**

Run: `pytest tests/web -v`
Expected: PASS

- [ ] **Step 7: Lint and type-check**

Run: `ruff format mediascanmonitor/web/pages.py && ruff check --fix mediascanmonitor/web/pages.py && mypy mediascanmonitor`
Expected: no errors (Jinja templates aren't linted/type-checked)

- [ ] **Step 8: Manual browser verification**

Per `CLAUDE.md`'s frontend-change rule: start the dev server (`scripts/dev_serve.sh`), open
`/servers/new` and confirm all four "Event types" checkboxes render checked; open an existing
server's detail page, uncheck one box, save, reopen the page, and confirm the unchecked box stays
unchecked (persisted correctly) while the others remain checked.

- [ ] **Step 9: Commit**

```bash
git add mediascanmonitor/web/pages.py mediascanmonitor/web/templates/_server_form_fields.html tests/web/test_pages.py
git commit -m "feat(web): render event-type checkboxes in the server form"
```

---

### Task 9: Docs

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: nothing (prose only).
- Produces: nothing consumed by later tasks — this is the last task.

- [ ] **Step 1: Update the routing description**

In `CLAUDE.md`, replace the sentence at `CLAUDE.md:90-92`:

```markdown
- **Routing:** the watch set is the deduplicated union of all enabled folder paths. On a
  filesystem event, every `(server, folder)` whose path is a prefix of the changed file **and**
  whose extensions match becomes a *subscriber*; the event fans out to each.
```

with:

```markdown
- **Routing:** the watch set is the deduplicated union of all enabled folder paths. On a
  filesystem event, every `(server, folder)` whose path is a prefix of the changed file, whose
  extensions match, **and** whose server is subscribed to the event's type (`created`/
  `moved_to`/`deleted`/`moved_from`, default: all four) becomes a *subscriber*; the event fans
  out to each.
```

- [ ] **Step 2: Verify the doc change renders sensibly**

Run: `git diff CLAUDE.md` and read it — confirm the sentence still parses cleanly and the rest of
the "Core concepts" bullet list is untouched.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note per-server event-type subscription in the routing description"
```

---

## Final verification

After Task 9's commit, run the full project gate once more from the repo root:

```bash
ruff check .
ruff format --check .
mypy mediascanmonitor
pytest
```

Expected: all four green. This is the same gate CI (`.github/workflows/ci.yml`) runs — a clean
local pass here means the branch is ready for the PR described in
`superpowers:finishing-a-development-branch`.
