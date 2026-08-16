"""RawEventsBus: same ring buffer + per-subscriber fan-out contract as EventsBus, one level
earlier in the pipeline (raw watcher events, before routing)."""

import asyncio
import dataclasses

import pytest

from mediascanmonitor.observ.events_bus import RawEventRecord, RawEventsBus


def make_record(n: int) -> RawEventRecord:
    return RawEventRecord(
        ts=f"2026-06-20T18:30:{n:02d}+00:00",
        event_type="created",
        path=f"/data/{n}/file.mkv",
        is_dir=False,
        matched=True,
    )


def test_raw_event_record_is_frozen() -> None:
    rec = make_record(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.matched = False  # type: ignore[misc]


def test_recent_returns_buffer_newest_last() -> None:
    bus = RawEventsBus(capacity=10)
    for n in range(3):
        bus.publish(make_record(n))
    recent = bus.recent()
    assert [r.path for r in recent] == ["/data/0/file.mkv", "/data/1/file.mkv", "/data/2/file.mkv"]


def test_recent_respects_capacity_and_limit() -> None:
    bus = RawEventsBus(capacity=2)
    for n in range(5):
        bus.publish(make_record(n))  # only the last 2 survive the ring
    assert [r.path for r in bus.recent()] == ["/data/3/file.mkv", "/data/4/file.mkv"]
    assert [r.path for r in bus.recent(limit=1)] == ["/data/4/file.mkv"]


def test_recent_on_empty_bus() -> None:
    assert RawEventsBus().recent() == []


async def test_subscribe_receives_published_record() -> None:
    bus = RawEventsBus()
    agen = bus.subscribe()
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    bus.publish(make_record(7))
    received = await asyncio.wait_for(task, timeout=1.0)
    assert received.path == "/data/7/file.mkv"
    await agen.aclose()


async def test_publish_never_blocks_when_subscriber_queue_full() -> None:
    bus = RawEventsBus()
    agen = bus.subscribe()
    await asyncio.sleep(0)
    pending = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)
    for n in range(5000):  # far exceeds the per-subscriber queue bound
        bus.publish(make_record(n % 100))
    first = await asyncio.wait_for(pending, timeout=1.0)
    assert isinstance(first, RawEventRecord)  # got *a* record; no deadlock
    await agen.aclose()
