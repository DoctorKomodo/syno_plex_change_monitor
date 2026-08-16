"""In-process event bus for the live feed (contract §G).

A bounded ``deque`` ring buffer keeps the most recent records for replay
(``recent``); each live subscriber gets its own bounded ``asyncio.Queue`` so a slow
SSE client can never block ``publish``. ``publish`` is sync and non-blocking — safe to
call from ``Engine._dispatch`` (wired in sub-plan 03). On a full subscriber queue the
OLDEST queued record is dropped (the client misses a beat) rather than blocking the
producer.

SECURITY: ``EventRecord`` carries no secret/token field (rule 5). Nothing here may
render a credential.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

_SUBSCRIBER_QUEUE_MAXSIZE = 100


@dataclass(frozen=True, slots=True)
class EventRecord:
    ts: str  # ISO-8601 UTC, e.g. "2026-06-20T18:30:00+00:00"
    server_id: int
    server_name: str
    scan_mode: str  # ScanMode value
    scan_key: str
    scan_path: str | None
    library_id: str | None
    event_type: str  # FsEventType value
    file_path: str
    ok: bool
    status_code: int | None
    detail: str


class EventsBus:
    def __init__(self, *, capacity: int = 200) -> None:
        self._buffer: deque[EventRecord] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue[EventRecord]] = set()

    def publish(self, record: EventRecord) -> None:
        self._buffer.append(record)
        for queue in self._subscribers:
            if queue.full():
                # drop the oldest queued item so publish never blocks on a slow subscriber
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover
                    queue.get_nowait()
            queue.put_nowait(record)

    def recent(self, limit: int = 50) -> list[EventRecord]:
        if limit <= 0:
            return []
        records = list(self._buffer)
        return records[-limit:]

    async def subscribe(self) -> AsyncIterator[EventRecord]:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


@dataclass(frozen=True, slots=True)
class RawEventRecord:
    ts: str  # ISO-8601 UTC
    event_type: str  # FsEventType value
    path: str
    is_dir: bool
    matched: bool  # True if routing produced at least one subscriber for this event


class RawEventsBus:
    """Same ring-buffer/fan-out shape as ``EventsBus``, one level earlier in the pipeline:
    the raw watcher event, published from ``Engine._handle_event`` before routing/debounce
    collapse it into per-server ``ScanRequest``s. Kept as its own class rather than sharing
    a generic base — the two record types are unrelated domains that may drift independently.
    """

    def __init__(self, *, capacity: int = 200) -> None:
        self._buffer: deque[RawEventRecord] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue[RawEventRecord]] = set()

    def publish(self, record: RawEventRecord) -> None:
        self._buffer.append(record)
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover
                    queue.get_nowait()
            queue.put_nowait(record)

    def recent(self, limit: int = 50) -> list[RawEventRecord]:
        if limit <= 0:
            return []
        records = list(self._buffer)
        return records[-limit:]

    async def subscribe(self) -> AsyncIterator[RawEventRecord]:
        queue: asyncio.Queue[RawEventRecord] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
