"""SSE /events/raw/stream: same auth-guard/replay/live contract as /events/stream, tested the
same way — see tests/web/test_sse.py for why the live path is driven through the generator
directly rather than through httpx's ASGITransport."""

import asyncio
import json

import httpx
import pytest

from mediascanmonitor.observ.events_bus import RawEventRecord, RawEventsBus
from mediascanmonitor.web.pages import _raw_event_generator, _raw_sse_frame


def _make_record(path: str = "/data/tv/Show/ep01.mkv", *, matched: bool = True) -> RawEventRecord:
    return RawEventRecord(
        ts="2026-06-20T18:30:00+00:00",
        event_type="created",
        path=path,
        is_dir=False,
        matched=matched,
    )


class _FakeRequest:
    """Minimal stand-in: ``_raw_event_generator`` only awaits ``request.is_disconnected()``."""

    def __init__(self, *, disconnected: bool = False) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


def test_events_raw_stream_requires_auth(client: httpx.Client) -> None:
    resp = client.get("/events/raw/stream", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_raw_sse_frame_format() -> None:
    frame = _raw_sse_frame(_make_record())
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload["path"] == "/data/tv/Show/ep01.mkv"
    assert payload["matched"] is True


async def test_raw_sse_replays_recent_records(raw_events_bus: RawEventsBus) -> None:
    raw_events_bus.publish(_make_record("/data/tv/One.mkv"))
    raw_events_bus.publish(_make_record("/data/tv/Two.mkv", matched=False))

    gen = _raw_event_generator(_FakeRequest(), raw_events_bus)  # type: ignore[arg-type]
    frames = [await anext(gen), await anext(gen)]
    await gen.aclose()

    parsed = [json.loads(f.removeprefix("data: ").strip()) for f in frames]
    assert [p["path"] for p in parsed] == ["/data/tv/One.mkv", "/data/tv/Two.mkv"]
    assert [p["matched"] for p in parsed] == [True, False]


async def test_raw_sse_streams_live_then_stops_on_disconnect(raw_events_bus: RawEventsBus) -> None:
    request = _FakeRequest(disconnected=False)
    gen = _raw_event_generator(request, raw_events_bus)  # type: ignore[arg-type]

    pending = asyncio.ensure_future(anext(gen))
    await asyncio.sleep(0)
    raw_events_bus.publish(_make_record("/data/tv/Live.mkv"))
    frame = await asyncio.wait_for(pending, timeout=1.0)
    assert json.loads(frame.removeprefix("data: ").strip())["path"] == "/data/tv/Live.mkv"

    request.disconnected = True
    raw_events_bus.publish(_make_record("/data/tv/AfterDisconnect.mkv"))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(gen), timeout=1.0)
    await gen.aclose()
