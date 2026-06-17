"""RedisEventBus (B5) — a networked EventBus over Redis Streams + a consumer group.

The decoupled/distributed `EventBus`: `publish` is `XADD` (durably accepted, returns
immediately); a background `run()` loop does `XREADGROUP` → dedupe → dispatch to the
subscribed handlers → `XACK`. Streams (not pub/sub) so events buffer and survive a consumer
being briefly absent, and delivery is at-least-once with explicit ack.

Mode note: because `publish` does NOT run handlers inline (unlike `InProcessEventBus`), this
bus only fits the decoupled `serve()` path — never the synchronous `Runtime.trigger()` path
that assumes `publish` populated the work queue before it returns. See the redis-broker plan.

Scope: durable, out-of-process transport with at-least-once delivery, ack-on-consume.
Crash-mid-run recovery (ack-on-completion, redelivering in-flight runs) is durable-Runtime
work (B10) and is out of scope here.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable

from loopy_runtime.bus.codec import decode_event, encode_event
from loopy_runtime.contract import Event, EventName, StateStore

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]

_DEFAULT_STREAM = "loopy:events"
_DEFAULT_GROUP = "loopy-workers"


class RedisEventBus:
    def __init__(
        self,
        url: str | None = None,
        *,
        stream: str = _DEFAULT_STREAM,
        group: str = _DEFAULT_GROUP,
        consumer: str | None = None,
        state: StateStore | None = None,
        client=None,
        batch: int = 64,
        block_ms: int = 1000,
    ) -> None:
        self._url = url
        self._stream = stream
        self._group = group
        self._consumer = consumer or f"c-{uuid.uuid4().hex[:8]}"
        self._state = state  # dedupe store (Event.id); optional but recommended
        self._client = client  # injectable (fakeredis) for tests
        self._batch = batch
        self._block_ms = block_ms
        self._subscribers: dict[EventName, list[Handler]] = defaultdict(list)
        self._ready = False
        self._stopped = False

    # ── EventBus Protocol ────────────────────────────────────────────────────────
    def subscribe(self, name: EventName, handler: Handler) -> None:
        self._subscribers[name].append(handler)

    async def publish(self, event: Event) -> None:
        await self._ensure()
        await self._client.xadd(self._stream, {"event": encode_event(event)})

    async def run(self) -> None:
        """Consume the stream and dispatch until cancelled/stopped (the server runs this as a
        task). Blocks up to `block_ms` per read so cancellation is responsive."""
        await self._ensure()
        try:
            while not self._stopped:
                await self._consume_once(block=self._block_ms)
        finally:
            await self.close()

    # ── Internals ──────────────────────────────────────────────────────────────────
    async def _ensure(self) -> None:
        """Connect (if no client was injected) and create the consumer group idempotently.
        The group is created at id=0 with mkstream, so events published before any consumer
        attaches are still delivered (the broker buffers)."""
        if self._client is None:
            import redis.asyncio as aioredis  # lazy: redis is an optional dependency

            self._client = aioredis.from_url(self._url, decode_responses=True)
        if not self._ready:
            try:
                await self._client.xgroup_create(
                    self._stream, self._group, id="0", mkstream=True
                )
            except Exception as exc:  # noqa: BLE001 - only BUSYGROUP is expected/benign
                if "BUSYGROUP" not in str(exc):
                    raise
            self._ready = True

    async def _consume_once(self, *, block: int | None = None) -> int:
        """Read one batch from the group, dispatch each entry, return how many were read.
        `block=None` is non-blocking (used by tests); `run()` passes `block_ms`."""
        resp = await self._client.xreadgroup(
            self._group, self._consumer, {self._stream: ">"}, count=self._batch, block=block
        )
        handled = 0
        for _stream, entries in resp or []:
            for msg_id, data in entries:
                await self._dispatch(msg_id, data)
                handled += 1
        return handled

    async def _dispatch(self, msg_id: str, data: dict) -> None:
        """Decode, dedupe by Event.id, fan out to subscribers, then XACK. A failed dispatch
        is left un-acked so it is redelivered; a duplicate (already seen) is acked and skipped."""
        try:
            event = decode_event(data["event"])
        except Exception:  # noqa: BLE001 - undecodable payload: ack to avoid a poison loop
            logger.exception("undecodable event on %s id=%s; dropping", self._stream, msg_id)
            await self._client.xack(self._stream, self._group, msg_id)
            return

        try:
            if self._state is not None and await self._state.seen(event.id):
                await self._client.xack(self._stream, self._group, msg_id)  # redelivery → skip
                return
            for handler in self._subscribers.get(event.name, []):
                await handler(event)
            if self._state is not None:
                await self._state.mark_seen(event.id)
            await self._client.xack(self._stream, self._group, msg_id)
        except Exception:  # noqa: BLE001 - leave un-acked for redelivery; don't kill the loop
            logger.exception("dispatch failed for %s id=%s; left pending", event.name, msg_id)

    async def close(self) -> None:
        self._stopped = True
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - best-effort close on shutdown
                logger.debug("redis client close failed", exc_info=True)
