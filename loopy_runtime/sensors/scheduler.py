"""In-process poll scheduler — the InMemory-equivalent for timers (behind the `Scheduler` seam).

Runs one asyncio task per poll sensor. Each task loops:

    scheduled_at = now()
    last_run     = await state.get_watermark(name)   # cold start: scheduled_at - interval
    events       = poll_fn(Tick(scheduled_at, last_run))
    for e in events: await receiver.receive(e)        # validate -> publish (the same gate)
    await state.set_watermark(name, scheduled_at)     # advance ONLY after delivery succeeds
    await sleep(interval)                             # then schedule the next tick

Two invariants the durable variant (B7) must also keep:
- **Sequential per sensor** — the next tick is scheduled *after* the current delivery
  completes (one coroutine awaiting in order), so a slow sensor can't overlap itself.
- **Watermark advances only on success** — if the poll fn raises, `last_run` stays put and
  the next tick retries the same window (no skipped data).

The clock and `sleep` are injected so tests are deterministic and never sleep for real.
Durability/restart-survival, single-firing locks, and cron intervals are out of scope here
(they drop in behind the `Scheduler` Protocol later).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from loopy_runtime.contract import EventReceiver, PollFn, StateStore, Tick, TriggerId

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
# A cron trigger's fire action: given the tick's `scheduled_at`, do the work (instantiate a
# run via `Runtime.tick`). Returns whatever the caller ignores — the loop only cares that it
# didn't raise. Kept async to mirror the poll path (the scheduler awaits downstream work).
CronFire = Callable[[datetime], Awaitable[object]]

_INTERVAL_RE = re.compile(r"(\d+)([smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_interval(spec: str) -> timedelta:
    """Parse a duration string (`"30s"`, `"5m"`, `"1h"`, `"2d"`) to a timedelta.

    Duration-only by design — poll sensors take durations; 5-field cron expressions ride the
    workflow `on: cron(...)` path (see `cron_next`/`cron_prev`). Raises ValueError otherwise."""
    match = _INTERVAL_RE.fullmatch(spec.strip())
    if not match:
        raise ValueError(f"invalid poll interval {spec!r} (expected e.g. '30s', '5m', '1h', '2d')")
    value, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(value)})


def cron_next(expr: str, after: datetime, tz: str | None = None) -> datetime:
    """The next cron occurrence strictly after `after`, returned in UTC.

    The expression is interpreted in `tz` (an IANA name) when given, else UTC — so `0 3 * * *`
    with `tz="America/New_York"` fires at 03:00 New York time, returned as the equivalent UTC
    instant. croniter validity/tz were already checked at compile time (E110)."""
    base = after.astimezone(ZoneInfo(tz)) if tz else after
    return croniter(expr, base).get_next(datetime).astimezone(UTC)


def cron_prev(expr: str, before: datetime, tz: str | None = None) -> datetime:
    """The previous cron occurrence strictly before `before`, returned in UTC — the cron
    analogue of a poll's `scheduled_at - interval` cold-start window (`last_run` on first fire)."""
    base = before.astimezone(ZoneInfo(tz)) if tz else before
    return croniter(expr, base).get_prev(datetime).astimezone(UTC)


@dataclass
class _Poll:
    name: TriggerId
    interval: timedelta
    poll_fn: PollFn


@dataclass
class _Cron:
    name: TriggerId
    expr: str
    tz: str | None
    fire: CronFire


class PollScheduler:
    """In-process asyncio poll scheduler. One task per registered sensor; reuses the
    existing `EventReceiver` -> `EventBus` -> `serve()` delivery path unchanged."""

    def __init__(
        self,
        *,
        receiver: EventReceiver,
        state: StateStore,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._receiver = receiver
        self._state = state
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._polls: list[_Poll] = []
        self._crons: list[_Cron] = []

    @property
    def poll_names(self) -> list[TriggerId]:
        return [p.name for p in self._polls]

    @property
    def cron_names(self) -> list[TriggerId]:
        return [c.name for c in self._crons]

    def register(self, name: TriggerId, interval: timedelta, poll_fn: PollFn) -> None:
        self._polls.append(_Poll(name, interval, poll_fn))

    def register_cron(self, name: TriggerId, expr: str, tz: str | None, fire: CronFire) -> None:
        """Register a workflow's `on: cron(...)` entry: call `fire(scheduled_at)` at every
        occurrence of `expr` (interpreted in `tz`). Unlike a poll, the fire action drives a run
        directly (no event/bus) — the watermark (`last_run`) is owned by `Runtime.tick`, so this
        loop is pure timing. The durable variant (B7) persists next-fire + a single-firing claim
        behind this same seam."""
        self._crons.append(_Cron(name, expr, tz, fire))

    async def start(self) -> None:
        """Run every registered poll and cron loop concurrently until cancelled."""
        if not self._polls and not self._crons:
            return
        loops = [self._run_loop(poll) for poll in self._polls]
        loops += [self._cron_run_loop(cron) for cron in self._crons]
        await asyncio.gather(*loops)

    async def _run_loop(self, poll: _Poll) -> None:
        """One sensor's loop: tick, then sleep to the next tick. A poll failure is logged
        and the watermark held; the loop survives so the next tick retries the window."""
        while True:
            try:
                await self._tick_once(poll)
            except Exception:  # noqa: BLE001 - a poll failure must not kill the loop
                logger.exception("poll sensor %r failed; watermark held", poll.name)
            await self._sleep(poll.interval.total_seconds())

    async def _tick_once(self, poll: _Poll) -> None:
        """Fire one tick: read the watermark, poll, deliver every event, then advance the
        watermark. If `poll_fn` raises, this propagates *before* the advance, so `last_run`
        stays put (the caller in `_run_loop` swallows it to keep looping)."""
        scheduled_at = self._clock()
        last_run = await self._state.get_watermark(poll.name)
        if last_run is None:  # cold start: scan exactly one window, not all history
            last_run = scheduled_at - poll.interval
        events = poll.poll_fn(Tick(scheduled_at=scheduled_at, last_run=last_run))
        for event in events:
            await self._receiver.receive(event)  # the validation gate, same as webhooks
        await self._state.set_watermark(poll.name, scheduled_at)

    async def _cron_run_loop(self, cron: _Cron) -> None:
        """One cron trigger's loop: sleep to the next occurrence, then fire it. Sleep-first
        (a cron names *when*, not *how often*), so each wake lands on a real occurrence. A fire
        failure is logged and the loop survives, like the poll loop."""
        while True:
            now = self._clock()
            scheduled_at = cron_next(cron.expr, now, cron.tz)
            await self._sleep((scheduled_at - now).total_seconds())
            try:
                await cron.fire(scheduled_at)
            except Exception:  # noqa: BLE001 - a fire failure must not kill the loop
                logger.exception("cron trigger %r fire failed", cron.name)
