# Poll sensors: an in-process scheduler behind a durable-timer seam

**Status:** draft (design ready — implement when scheduled)
**Owner:** peter
**Date:** 2026-06-17

## Goal
Make poll sensors an actually-runnable trigger: run each `@sensor(poll=…)` on a schedule, hand it a
**`Tick`** (`scheduled_at`, `last_run`), normalize its returned event(s), and deliver them through
the `EventReceiver` → `EventBus` → `serve()` path already built. Polling is the intended near-term
sensor model (webhooks deferred — see `BACKLOG.scratch.md` B and the sensor-ingress plan), so this
closes the gap the docs now flag ("polling is the direction, not yet runnable").

## Context — what already exists (keeps this small)
- `@sensor(webhook=None, poll=None, emits=…)` — the decorator already accepts `poll` (`authoring.py`).
- `SensorSpec.trigger` already models `kind="poll"` + `interval` (manifest).
- `StateStore.get_watermark` / `set_watermark` exist (currently unused) — the `last_run` store.
- Delivery is built: `EventReceiver` (validate) → `EventBus.publish` → `Runtime.serve()` drains.
  Poll events are just another producer feeding it; nothing downstream changes.
- `croniter` is already a dependency.
- Missing: the **scheduler**, the **`Tick` input type**, **multi-event** support, and **CLI wiring**.

## Key design decisions (the reasoning to preserve)

### 1. Poll input is a `Tick`, NOT the webhook `Request`
Webhook and poll sensors share their **output** (both return events) but not their **input**:
- webhook ← an inbound HTTP request → `Request` (`req.json` is the body)
- poll ← a scheduler tick → **`Tick`** (`tick.scheduled_at`, `tick.last_run`); no request, no body
Handing a poll sensor a faked `Request` would abuse a webhook object for something it isn't. What's
*shared* is only the machinery (import fn → normalize return to events → deliver via receiver); the
input wrapper differs by trigger kind.
```python
@sensor(poll="5m", emits="DepAlert")
def scan_deps(tick) -> Iterator[DepAlert]:        # tick.last_run / tick.scheduled_at
    for cve in cves_since(tick.last_run):          # incremental scan since the watermark
        yield DepAlert(...)
```

### 2. A tick is three steps; a message broker only helps with one
1. **Fire the tick** (decide "it's time") — needs a timer source.
2. **Read/advance the watermark** (`last_run`) — needs durable KV (the `StateStore`).
3. **Deliver produced events** — the `EventReceiver` → `EventBus`.

A **message broker (pub/sub) does NOT fire timers** — it only helps step 3 (delivery), which already
leans on the `EventBus` (networked broker ⇒ durable, out-of-process delivery, free). Steps 1–2 are
the stateful part a broker doesn't remove. "Lean on Redis" really means Redis in up to *three* roles
(streams = delivery/`EventBus`, KV = watermark/`StateStore`, sorted-set + `SETNX` lock = a
hand-rolled durable timer) — only the first is the broker role. So don't conflate delivery with
timing.

### 3. The scheduler is a swappable module with a durability target
Like `Runtime` and `StateStore`: ship the **in-process asyncio** scheduler now (the InMemory-
equivalent for timers), behind a seam so a durable backing drops in at the **B7** milestone.
Crucially — if/when a durable-execution `Runtime` is adopted (**DBOS / Temporal**), durable timers
come as a *native primitive*, so we'd likely **never hand-roll the Redis zset**. The durability
choice (Redis vs Postgres vs Temporal timers) is made at B7, not baked in now. The seam to design
for is "durable timer + watermark," not anything Redis-specific.

## Approach
Add a standalone `PollScheduler` (no uvicorn needed — relevant now that webhooks are deferred) that
runs one task per poll sensor and reuses the existing delivery path. Generalize sensor returns to
multiple events (polls fan out). Keep the timer/watermark behind interfaces so durability swaps in.

```
loop (per poll sensor):
  scheduled_at = now()
  last_run     = await state.get_watermark(sensor.name)
  events       = poll_fn(Tick(scheduled_at, last_run))     # normalized to list[Event]
  for e in events: await receiver.receive(e)               # validate → publish (the gate)
  await state.set_watermark(sensor.name, scheduled_at)     # advance ONLY after success
  await sleep_until_next(interval)
```
- **Sequential per sensor:** schedule the next tick *after* the current delivery completes — a slow
  sensor can't overlap itself.
- **Watermark advances only on success:** if the poll fn raises, `last_run` stays put; next tick
  retries the same window (no skipped data).
- **Cold start:** first tick uses `last_run = scheduled_at − interval` (scan one window, not all
  history).

## Steps
- [ ] `Tick` value type (`scheduled_at: datetime`, `last_run: datetime | None`) in the runtime contract.
- [ ] Generalize sensor-return normalization: `to_events(result) -> list[Event]` handling
      `None | model | Iterable[model]` (replaces the current `to_event` that raises on iterables).
      Also unlocks the README's `yield`-an-iterator webhook sensors.
- [ ] `load_poll_sensor(spec, root)` — mirror `load_webhook_sensor`, but pass a `Tick` (not `Request`)
      and normalize to events.
- [ ] `PollScheduler` — asyncio task per sensor; loop above; behind a small seam
      (a `Scheduler`/timer protocol) so APScheduler / durable-timer variants swap in.
- [ ] Interval parsing: duration strings (`"30s"`, `"5m"`, `"1h"`) → seconds.
- [ ] `loopy run`: iterate **poll** sensors, parse intervals, register with the `PollScheduler`, run
      `scheduler.start()` alongside `runtime.serve()`. (Webhook wiring stays as the deferred path.)
- [ ] Tests: a poll fn fans out N events → N runs via the receiver; watermark advances on success and
      holds on failure; cold-start window; sequential (no overlap); a stub clock so tests don't sleep.

## Files likely to change
- `loopy_runtime/contract.py` — `Tick`; maybe a `Scheduler` protocol.
- `loopy_runtime/sensors/loader.py` — `to_events` generalization; `load_poll_sensor`.
- new `loopy_runtime/sensors/scheduler.py` — `PollScheduler` (in-process asyncio).
- `loopy_cli/__init__.py` — wire poll sensors + start the scheduler.
- tests under `tests/`.

## Constraints & non-goals
**Constraints**
- Reuse the receiver → bus → `serve()` delivery path unchanged; poll events go through the validation gate.
- Timer + watermark stay behind interfaces (durability swaps in later, not Redis-specific).
- Testable without real time — inject the clock / `sleep` so tests are deterministic and fast.

**Non-goals (deferred)**
- **Durability / restart-survival (B7):** v1 scheduler is process-lifetime and the in-memory
  `StateStore` loses watermarks on restart → intervals re-start, `last_run` resets, no missed-tick
  catch-up. Durable backing (Redis zset+lock / Postgres / Temporal-or-DBOS native timers) drops in
  behind the seam later.
- **Multi-instance HA / single-firing** (the lock) — part of the durable-timer work.
- **Cron-expression intervals** — duration-only now; cron via `croniter` lands with the workflow
  `on: cron(...)` work.
- **Workflow `on: cron(...)` triggers / `Runtime.tick`** — the *same scheduler* can later fire these
  (closing that `NotImplementedError`), but this change is scoped to poll **sensors**.

## Open questions
- `Scheduler` seam shape: how thin? Minimum is "given (sensor, interval), call back on each tick";
  the durable variant needs to own next-fire persistence + the claim/lock. Define the interface so
  the in-process and durable versions both satisfy it without leaking Redis/Temporal specifics.
- Watermark granularity: per-sensor (proposed: keyed by `sensor.name`) vs per-(sensor, shard).

## Notes / decisions
- 2026-06-17: Corrected the input contract from `Request` to a purpose-built `Tick` (webhook vs poll
  share output, not input). Established that a broker helps delivery (the `EventBus`) but not the
  timer; the scheduler is a swappable module with a durability target, and durable timers most likely
  come from an adopted durable `Runtime` (DBOS/Temporal) rather than a hand-rolled Redis zset.
  Decided: in-process asyncio scheduler now, behind a durable-timer + watermark seam; webhooks remain
  deferred so polling is the primary ingress. Implementation deferred — this plan is the record.
