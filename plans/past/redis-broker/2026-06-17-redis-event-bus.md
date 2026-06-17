# Redis as an initial message broker (`RedisEventBus`)

**Status:** done (RedisEventBus shipped behind the EventBus seam; durable-run recovery still B10)
**Owner:** peter
**Date:** 2026-06-17

## Goal
Ship a **networked `EventBus`** backed by Redis so events travel out-of-process, are buffered, and
are delivered at-least-once — realizing the "decoupled (distributed)" topology that
`ARCHITECTURE.md §3.2` and `DEPLOYMENT.md §3` already describe ("swap the in-process bus for a
broker without touching a tier above or below"). This is purely a Tier-3 (the seam) change.

## Context — what already exists (keeps this small)
- `EventBus` Protocol = `publish(event)` + `subscribe(name, handler)`; `Event` is a JSON-serializable
  frozen dataclass. `InProcessEventBus` keeps a `name -> [handlers]` dict and **runs handlers
  synchronously inside `publish`**.
- The runtime subscribes one handler per workflow at construction; each handler just enqueues
  `(workflow, event)` onto the runtime's local deque and sets `self._work`. `serve()` drains on each
  wake. The receiver re-validates every event before it reaches the bus (the gate is already there).
- `StateStore.seen` / `mark_seen` exist and are currently unused — the dedupe store for at-least-once.
- `DEPLOYMENT.md §3.2` lists "an external broker" as the deferred/speculative item this closes.

## Key design decision (the reasoning to preserve)
**`publish`'s contract splits across implementations, and that is the whole obstacle.** In-process,
`publish` means *"delivered + enqueued"* (handlers ran synchronously); over a broker it can only mean
*"durably accepted"* — the matching handler fires later, from a background consumer reading the
stream. One caller leans on the synchronous meaning: `Runtime.trigger()` does
`await bus.publish(event)` then immediately `await self._drain()`, assuming the queue is now
populated. A networked bus cannot honor that.

This is not a rewrite — it is a boundary the architecture already drew. There are exactly two modes:
- **Synchronous (in-process):** `publish` runs handlers inline; `loopy trigger` (one-shot) lives here.
- **Decoupled (distributed):** `publish` writes to the broker and returns; a long-lived consumer
  feeds the runtime later; `loopy run` / `serve()` lives here.

**The `RedisEventBus` *is* the decoupled mode.** It powers `loopy run`; `loopy trigger` stays on the
in-process bus. No `Runtime` change. We document the `publish` split in `ARCHITECTURE.md` so nobody
later wires a synchronous caller onto a networked bus and hits an empty-queue bug.

## Approach
- **Redis Streams + a consumer group** (`XADD` / `XREADGROUP` / `XACK`), not pub/sub. Streams give
  durability, buffering, and at-least-once delivery with explicit ack; pub/sub is fire-and-forget and
  drops events when no consumer is connected — which defeats the purpose. Group created at `id=0`
  with `mkstream` so events published before a consumer attaches are still delivered (the buffering
  property).
- **One stream** (`loopy:events`); the consumer reads everything and dispatches to handlers
  registered by event **name** in the *same* `_subscribers` dict the in-proc bus uses. Fan-out and
  loop-backs keep working unchanged — a step's `emits:` round-trips through the stream (verified by
  the conformance-style test below).
- **Event codec** (`Event` ⇄ JSON): `emitted_at` as ISO-8601, `fields`/`id`/`name` as JSON. The
  receiver re-validates after decode, so untrusted bytes off the wire hit the existing gate.
- **At-least-once ⇒ dedupe by `Event.id`** using `StateStore.seen` / `mark_seen` at the consume
  boundary; `XACK` only after the event is safely handed to the runtime. A failed dispatch is left
  un-acked for redelivery.
- **Lifecycle:** add an additive `async def run(self)` consume loop to the `EventBus` Protocol
  (no-op on the in-proc bus). The CLI runs it as a task alongside `runtime.serve()` and
  `scheduler.start()` — the same pattern the poll scheduler established.

## Honest scope boundary
Delivers **durable, out-of-process event transport with at-least-once delivery and ack-on-consume.**
It does **not** deliver crash-mid-run recovery: `XACK` happens when the event is handed to the
runtime, not when the run completes. Recovering in-flight *runs* after a crash is durable-`Runtime` /
`StateStore` work (B10) and stays out of scope.

## Steps
- [x] `loopy_runtime/bus/codec.py` — `encode_event` / `decode_event` (+ round-trip tests).
- [x] `loopy_runtime/contract.py` — additive `async def run(self) -> None` on the `EventBus` Protocol.
- [x] `loopy_runtime/bus/inproc.py` — no-op `run()` (delivery already happens in `publish`).
- [x] `loopy_runtime/bus/redis.py` — `RedisEventBus`: `publish` (XADD), `subscribe`, `run()` /
      `_consume_once()` (XREADGROUP → dedupe → dispatch → XACK), idempotent group create, `close()`.
      Client injectable for tests.
- [x] `loopy_runtime/bus/factory.py` — `make_event_bus(name, redis_url, state)` (lazy redis import).
- [x] `loopy_cli/__init__.py` — `run` gains `--bus inproc|redis` + `--redis-url`; shares one
      `StateStore` across the runtime and the bus's dedupe; starts the consumer task. `trigger` stays
      in-process (synchronous mode), documented — not an option.
- [x] Tests (fakeredis, deterministic): codec round-trip; publish→consume→dispatch; dedupe on
      redelivery; XACK / pending; undecodable-payload guard; fan-out + loop-back conformance cascade.
- [x] Opt-in real-Redis integration test gated on `REDIS_URL` (skipped by default).
- [x] Docs: `DEPLOYMENT.md §3.2` external-broker → shipped; recorded the `publish` split + Streams
      rationale in `ARCHITECTURE.md` (EventBus row + §3.2 note + §3.4 `run()` signature).

## Outcome (2026-06-17)
Shipped behind the existing `EventBus` seam with **no `Runtime`, receiver, scheduler, or DAG-walk
change**. Validated `fakeredis.aioredis` covers XADD/XGROUP/XREADGROUP/XACK/XPENDING, so the real
bus code paths run offline; the §3.3 incidents cascade (incl. emits/loop-backs) round-trips through
the stream and matches the in-proc order exactly. One additive Protocol method (`run()`), one
conceptual decision recorded: `publish` = *"durably accepted,"* not *"delivered,"* so the networked
bus is `serve()`-mode only (never the synchronous `trigger` path). At-least-once + dedupe by
`Event.id` via the previously-unused `StateStore.seen`/`mark_seen`. Out of scope (unchanged):
crash-mid-run recovery / ack-on-completion (durable `Runtime`, B10).

## Files likely to change
- `loopy_runtime/bus/{codec,redis,factory}.py` (new), `loopy_runtime/bus/inproc.py`,
  `loopy_runtime/contract.py`, `loopy_cli/__init__.py`, `pyproject.toml` (redis optional + fakeredis
  dev), `tests/test_b5_redis_bus.py` (new), docs.

## Open questions (resolved for v1)
- Streams vs pub/sub → **Streams** (durable; the point of a broker).
- Test backing → **fakeredis** for deterministic CI; real Redis behind an opt-in `REDIS_URL` marker.
- Dedupe ownership → **bus consume boundary** via `StateStore.seen` (id-keyed, separate keyspace).
- Protocol `run()` → **additive**, no-op on in-proc (keeps one bus lifecycle shape).

## Notes / decisions
- 2026-06-17: Validated `fakeredis.aioredis` supports XADD/XGROUP/XREADGROUP/XACK/XPENDING, so the
  real code paths are testable offline. Confirmed the only seam tension is `publish`'s synchronous
  meaning (`Runtime.trigger`); scoped the Redis bus to decoupled/`serve()` mode rather than changing
  the Runtime.
