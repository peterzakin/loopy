"""B5 RedisEventBus — networked EventBus over Redis Streams, exercised against fakeredis.

fakeredis.aioredis implements XADD/XGROUP/XREADGROUP/XACK/XPENDING, so the real bus code
paths run offline and deterministically. `_consume_once(block=None)` is non-blocking, so each
test reads exactly what's on the stream without real time. An opt-in test at the bottom runs
the same flow against a real server when REDIS_URL is set.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopy_runtime.bus.codec import decode_event, encode_event
from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.bus.redis import RedisEventBus
from loopy_runtime.contract import Event, EventBus
from loopy_runtime.manifest_model import load_manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.state.inmemory import InMemoryStateStore
from tests.stub_harness import StubAgentHarness

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def _fake():
    import fakeredis.aioredis as fr

    return fr.FakeRedis(decode_responses=True)


def _event(name: str = "Thing", fields: dict | None = None, id: str = "e1") -> Event:
    return Event(name=name, fields=fields or {}, id=id, emitted_at=datetime.now(UTC))


def _incident(id: str = "trigger") -> Event:
    return Event(
        name="Incident",
        fields={
            "source": "sentry",
            "issue_id": "ISS-1",
            "title": "boom",
            "link": "https://example.test/i/1",
        },
        id=id,
        emitted_at=datetime.now(UTC),
    )


# ── codec ────────────────────────────────────────────────────────────────────────
def test_codec_round_trips():
    ev = _event("Thing", {"count": 3, "kind": "a", "ok": True}, id="abc")
    back = decode_event(encode_event(ev))
    assert back.name == ev.name
    assert back.fields == ev.fields
    assert back.id == ev.id
    assert back.emitted_at == ev.emitted_at


def test_both_buses_satisfy_the_protocol():
    assert isinstance(InProcessEventBus(), EventBus)
    assert isinstance(RedisEventBus(client=_fake()), EventBus)


# ── publish → consume → dispatch ───────────────────────────────────────────────────
def test_publish_then_consume_dispatches_to_subscriber():
    async def go():
        bus = RedisEventBus(client=_fake(), state=InMemoryStateStore())
        seen: list[str] = []
        bus.subscribe("Thing", lambda e: seen.append(e.name) or asyncio.sleep(0))
        await bus.publish(_event("Thing"))
        assert seen == []  # NOT delivered inline — the broker is async (unlike in-proc)
        n = await bus._consume_once()
        assert n == 1
        assert seen == ["Thing"]

    asyncio.run(go())


def test_only_matching_subscribers_are_called():
    async def go():
        bus = RedisEventBus(client=_fake(), state=InMemoryStateStore())
        thing: list[Event] = []
        other: list[Event] = []

        async def on_thing(e):
            thing.append(e)

        async def on_other(e):
            other.append(e)

        bus.subscribe("Thing", on_thing)
        bus.subscribe("Other", on_other)
        await bus.publish(_event("Thing"))
        await bus._consume_once()
        assert [e.name for e in thing] == ["Thing"]
        assert other == []

    asyncio.run(go())


def test_events_published_before_consumer_attaches_still_deliver():
    # Group is created at id=0 + mkstream, so the broker buffers — nothing is lost.
    async def go():
        client = _fake()
        producer = RedisEventBus(client=client)
        await producer.publish(_event("Thing", id="buffered"))

        consumer = RedisEventBus(client=client, state=InMemoryStateStore())
        got: list[str] = []
        consumer.subscribe("Thing", lambda e: got.append(e.id) or asyncio.sleep(0))
        await consumer._consume_once()
        assert got == ["buffered"]

    asyncio.run(go())


# ── at-least-once: dedupe + ack ────────────────────────────────────────────────────
def test_redelivered_event_is_deduped_by_id():
    async def go():
        client = _fake()
        bus = RedisEventBus(client=client, state=InMemoryStateStore())
        calls: list[str] = []

        async def handler(e):
            calls.append(e.id)

        bus.subscribe("Thing", handler)
        await bus.publish(_event("Thing", id="dup"))
        await bus._consume_once()
        # Same Event.id arrives again (a redelivery / duplicate producer): must NOT re-run.
        await bus.publish(_event("Thing", id="dup"))
        await bus._consume_once()
        assert calls == ["dup"]  # handled exactly once

    asyncio.run(go())


def test_successful_dispatch_acks_so_nothing_stays_pending():
    async def go():
        client = _fake()
        bus = RedisEventBus(client=client, state=InMemoryStateStore())
        bus.subscribe("Thing", lambda e: asyncio.sleep(0))
        await bus.publish(_event("Thing"))
        await bus._consume_once()
        pending = await client.xpending(bus._stream, bus._group)
        assert pending["pending"] == 0  # acked

    asyncio.run(go())


def test_failed_dispatch_is_left_pending_for_redelivery():
    async def go():
        client = _fake()
        bus = RedisEventBus(client=client, state=InMemoryStateStore())

        async def boom(e):
            raise RuntimeError("handler down")

        bus.subscribe("Thing", boom)
        await bus.publish(_event("Thing"))
        await bus._consume_once()  # raises inside dispatch, swallowed; not acked
        pending = await client.xpending(bus._stream, bus._group)
        assert pending["pending"] == 1  # still owed → will be redelivered

    asyncio.run(go())


def test_undecodable_payload_is_acked_not_a_poison_loop():
    async def go():
        client = _fake()
        bus = RedisEventBus(client=client, state=InMemoryStateStore())
        await bus._ensure()
        await client.xadd(bus._stream, {"event": "not-json{{"})
        n = await bus._consume_once()
        assert n == 1
        pending = await client.xpending(bus._stream, bus._group)
        assert pending["pending"] == 0  # dropped + acked, won't wedge the consumer

    asyncio.run(go())


# ── fan-out + loop-back through the stream (conformance-style) ─────────────────────
def _runtime(bus, state):
    m = load_manifest(GOLDEN)
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
        state=state,
    )


async def _pump(bus: RedisEventBus, rt: InMemoryRuntime) -> None:
    """Drive the decoupled path deterministically: consume stream → runtime queue, then drain
    (whose emits XADD back onto the stream), until a pass reads nothing new."""
    for _ in range(50):  # generous cap; the incidents cascade settles in a handful of passes
        consumed = await bus._consume_once()
        await rt.drain()
        if consumed == 0:
            return
    raise AssertionError("cascade did not settle")


def test_incident_cascade_round_trips_through_redis_streams():
    # The SAME conformance cascade as the in-proc bus, but every event (incl. emits/loop-backs)
    # round-trips through the Redis stream. Proves the bus is a genuine drop-in for the seam.
    async def go():
        state = InMemoryStateStore()
        bus = RedisEventBus(client=_fake(), state=state)
        rt = _runtime(bus, state)  # subscribes one handler per workflow onto the bus
        await bus.publish(_incident())
        await _pump(bus, rt)
        assert rt.execution_log == [
            "triage/investigate",
            "resolve/arbitrate",
            "resolve/fix",
            "resolve/review",
            "resolve/ship",
        ]
        assert rt.emitted_log == ["WorkItem", "GoalShipped"]

    asyncio.run(go())


# ── opt-in: run the same flow against a real Redis when REDIS_URL is set ────────────
@pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="set REDIS_URL to test real Redis")
def test_real_redis_publish_consume():  # pragma: no cover - opt-in, needs a live server
    async def go():
        import uuid

        bus = RedisEventBus(
            os.environ["REDIS_URL"],
            stream=f"loopy:test:{uuid.uuid4().hex}",
            state=InMemoryStateStore(),
        )
        got: list[str] = []
        bus.subscribe("Thing", lambda e: got.append(e.id) or asyncio.sleep(0))
        try:
            await bus.publish(_event("Thing", id="real"))
            await bus._consume_once()
            assert got == ["real"]
        finally:
            await bus.close()

    asyncio.run(go())
