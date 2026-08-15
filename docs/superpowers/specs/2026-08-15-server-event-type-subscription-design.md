# Per-server event-type subscription — design

**Date:** 2026-08-15
**Status:** approved (brainstorming)
**Topic:** Let each server pick which filesystem event types (`created` / `moved_to` /
`deleted` / `moved_from`) it wants routed to it. Default: all four (today's behaviour).

## Motivation

Routing already filters by folder path and file extension, but every matching event fans out
to a server regardless of *type*. Driving case: a generic webhook target that only cares about
new/moved-in files has no use for `deleted`/`moved_from` events, but today it receives (and
must itself ignore) all four. The fix is generic — not webhook-specific — so any server type
can narrow its subscription the same way.

## Decisions (locked during brainstorming)

1. **Per-server, not per-folder.** Mirrors `debounce_mode`/`scan_mode`/`retry_attempts` — one
   set of event types per server, applied regardless of which folder triggered the event.
   Extensions stay per-folder (unchanged); this is a separate, orthogonal filter.
2. **Default is all four selected**, both for new servers (schema default) and existing rows
   (migration `server_default`) — zero behaviour change until an operator opts out.
3. **Empty selection is allowed**, no UI/schema validation requiring at least one. Unchecking
   everything just makes the server go quiet (same practical effect as disabling it, via a
   different toggle) — consistent with how an empty `extensions` set is already a valid,
   meaningful state (there it means "match all"; here it means "match none" — different
   semantics, same "no artificial floor" philosophy).

## Design

### Enum relocation: `FsEventType` moves to `db/models.py`

`FsEventType` currently lives in `pipeline/events.py` because nothing persisted it. Now that
`Server` needs to store a set of these values, it must live where `ScanMode`/`DebounceMode`/
`WebhookPreset` already live — `pipeline/events.py` already imports `ScanMode` *from*
`db.models`, never the reverse, and `db.models` has zero upward dependencies. Moving the enum
there (same four members, same string values) keeps that layering intact.

```python
# db/models.py, beside ScanMode/DebounceMode
class FsEventType(StrEnum):
    created = "created"
    moved_to = "moved_to"
    deleted = "deleted"
    moved_from = "moved_from"
```

`pipeline/events.py` replaces its own definition with
`from mediascanmonitor.db.models import FsEventType` (re-exported under the same name), so
`watcher/inotify_backend.py`'s existing `from mediascanmonitor.pipeline.events import FsEvent,
FsEventType` keeps working unchanged.

**Parse/serialize helpers**, colocated with the enum in `db/models.py` (small, no new module —
only ~4 call sites):

```python
def serialize_event_types(types: Iterable[FsEventType]) -> str:
    return ",".join(t.value for t in types)

def parse_event_types(raw: str) -> frozenset[FsEventType]:
    return frozenset(FsEventType(v) for v in raw.split(",") if v)
```

### Data model + migration

`Server.event_types: str` — NOT NULL, comma-separated (e.g.
`"created,moved_to,deleted,moved_from"`). A plain string column, not a child table like
`FileType`: `FileType` is its own table specifically so the `Server > Folder > FileType`
cascade delete is independently testable (per its docstring); this is a single-level,
non-cascading attribute of `Server`, so a delimited string is proportionate and matches how
freeform config (`webhook_headers_json`) is already stored as text.

Migration `migrations/versions/0004_server_event_types.py`, `down_revision = "0003"`, mirrors
`0003`'s shape:

```python
def upgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_types",
                sa.String(),
                nullable=False,
                server_default="created,moved_to,deleted,moved_from",
            )
        )

def downgrade() -> None:
    with op.batch_alter_table("server", schema=None) as batch_op:
        batch_op.drop_column("event_types")
```

The `server_default` gives every pre-existing row "all four" with no manual backfill.

### Schemas (`db/schemas.py`)

`ServerCreate` / `ServerUpdate` gain:

```python
event_types: list[FsEventType] = Field(default_factory=lambda: list(FsEventType))
```

(`ServerUpdate`'s stays `list[FsEventType] | None = None`, following its all-optional
partial-update convention.)

### Repo (`db/repo.py`)

`create_server` / `create_server_with_folders` pass
`event_types=serialize_event_types(data.event_types)` into the `Server(...)` constructor,
alongside the other direct field assignments.

`update_server` / `update_server_with_folders` currently do a blanket
`for key, value in fields.items(): setattr(server, key, value)` over
`data.model_dump(exclude_unset=True)` — a bare `list[FsEventType]` would be set directly onto
the `str` column, which is wrong. `ServerUpdate.event_types` is typed `list[FsEventType] | None
= None` (the same optional-field shape `FolderUpdate.extensions` already uses), so an explicit
JSON `{"event_types": null}` on the `PATCH /api/servers/{id}` route reaches the repo as
`None`, same as an omitted field. Special-case it the same way `update_folder` already
special-cases `extensions` (`db/repo.py:209-221`, comment: *"explicit None is a no-op"*) — pop
it before the loop, and only convert/assign when a real list was sent:

```python
new_event_types = fields.pop("event_types", None)
for key, value in fields.items():
    setattr(server, key, value)
if new_event_types is not None:
    server.event_types = serialize_event_types(new_event_types)
```

This is a deliberate choice, not an oversight: it's the same tri-state precedent already used
for `extensions` in this codebase, applied consistently rather than inventing a new
null-rejection error path for this one field. (An earlier draft of this spec special-cased
`event_types` the same way `secret` is special-cased — `secret: None` legitimately means
"clear it" — but `event_types` has no such "clear via null" semantic; `[]` already means
"subscribe to nothing," so treating explicit null as a no-op, matching `extensions`, is the
correct precedent to follow, not `secret`.)

### `ServerRead` (`web/api_schemas.py`)

Add `event_types: list[FsEventType]`, in enum-declaration order; `from_model` populates it with
`[t for t in FsEventType if t in parse_event_types(server.event_types)]`. Not sensitive, no
redaction change.

### Routing (`config/runtime.py`, `pipeline/router.py`) — the actual filter

`FolderRoute` gains `event_types: frozenset[FsEventType]`. `build_runtime_config` denormalizes
it from the owning `Server` row onto each of that server's routes — same place `extensions` is
already denormalized from `Folder`'s filetypes onto the route:

```python
routes.append(
    FolderRoute(
        ...,
        event_types=parse_event_types(server.event_types),
    )
)
```

Note: this does **not** need to go on `ServerRuntime` — adapters never need to know their own
event-type filter, because filtering happens entirely upstream in the router before a
`ScanRequest` is ever built. `runtime_from_server`/`runtime_from_create`
(`web/serverprobe.py`) are untouched.

`pipeline/router.py`'s `route()` adds one more skip condition, in the same spot as the
existing extension check:

```python
if not extension_matches(event.path, folder_route.extensions):
    continue
if event.event_type not in folder_route.event_types:
    continue
```

A filtered-out event never becomes a `ScanRequest` for that server: no debounce-window entry,
no dispatch, no live-feed entry. This keeps the watcher/pipeline fully generic — no backend
special-casing (CLAUDE.md rule 2).

### Web UI

**`web/pages.py`:**
- A module-level label map next to the other option lists (`debounce_modes` etc.):
  ```python
  _EVENT_TYPE_LABELS: dict[FsEventType, str] = {
      FsEventType.created: "Created",
      FsEventType.moved_to: "Moved in",
      FsEventType.deleted: "Deleted",
      FsEventType.moved_from: "Moved out",
  }
  ```
  Both `GET /servers/new` and `GET /servers/{id}` pass
  `"event_type_options": list(_EVENT_TYPE_LABELS.items())` into the template context, next to
  where `debounce_modes` is already set.
- `ui_create_server_with_folders` and `ui_update_server` both already call
  `form = await request.form()` (for `_parse_folder_rows`). Add
  `event_types=[FsEventType(v) for v in form.getlist("event_types")]` into the `ServerCreate(...)`
  call / the `fields` dict, using the standard HTML same-name-checkbox-group pattern
  (`form.getlist`) rather than a typed `Form(...)` parameter — consistent with how folder rows
  are already parsed off the raw `form` object. An unchecked box is simply absent from
  `getlist`, so "select none" naturally produces `[]`.
- `ui_test_server_config` (the unsaved "Test connection" probe) is **not** touched — event-type
  filtering only affects routing, not connectivity, so the Test button has nothing to do with
  it (unlike `webhook_payload_preset`, which the adapter itself reads).

**`_server_form_fields.html`:** new checkbox group in the existing "Delivery" fieldset
(alongside Scan mode / Debounce mode — the natural home, since this is also "which events feed
delivery"):

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

`creating` defaults every box to checked (all four); edit mode checks a box iff that type is
in the saved `server.event_types`.

## Testing (TDD)

- **enum relocation:** existing `pipeline/events.py` tests keep passing unchanged (same values,
  re-exported name) — no new test needed here, just confirms nothing broke.
- **router:** extend `tests/pipeline/test_router.py` / `tests/pipeline/factories.py` —
  `make_folder_route` gains `event_types: frozenset[FsEventType] = frozenset(FsEventType)`
  (default all, so every existing test is unaffected). New cases: a route whose `event_types`
  excludes the incoming event's type produces zero `ScanRequest`s; a route that includes it
  behaves as today; empty `event_types` matches nothing.
- **schema/repo:** `ServerCreate`/`ServerUpdate` round-trip `event_types` through
  `create_server`/`update_server`; default on create is all four; update can narrow to a subset
  and later widen back; empty list round-trips as empty (not silently defaulted); an explicit
  `event_types=None` passed to `update_server` (simulating a JSON `null`) is a no-op and leaves
  the stored value unchanged, mirroring the existing `extensions`-null test coverage.
- **migration:** extend `tests/db/test_migrations.py` mirroring
  `test_server_table_has_webhook_payload_preset_column` /
  `test_webhook_payload_preset_server_default_is_custom` — assert the `event_types` column
  exists, and that a row inserted without it reads back
  `"created,moved_to,deleted,moved_from"`.
- **pages/UI:** the Delivery fieldset renders four checkboxes; new-server defaults all checked;
  edit page pre-checks exactly the saved set; the form parse maps `getlist("event_types")` into
  the schema for both create and update; unchecking all four saves an empty set without error.

## Docs

- `CLAUDE.md`'s routing description ("every `(server, folder)` whose path is a prefix... and
  whose extensions match becomes a subscriber") gets one clause added for event-type matching.
- No `FOLLOWUPS.md` entry — this is net-new, complete work, not a deferred stub.

## Out of scope (YAGNI)

- Per-folder event-type overrides (locked as per-server; see Decision 1).
- A "select all / none" quick-toggle control in the UI — four checkboxes need no shortcut.
- Any change to how `is_dir` synthetic `created` events (new-subdirectory backfill) are
  generated — they still fire as `created` and are subject to the same per-server filter as any
  other `created` event.
