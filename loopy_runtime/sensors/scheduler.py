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

from loopy_runtime.contract import EventReceiver, PollFn, StateStore, Tick, TriggerId

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

_INTERVAL_RE = re.compile(r"(\d+)([smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_interval(spec: str) -> timedelta:
    """Parse a duration string (`"30s"`, `"5m"`, `"1h"`, `"2d"`) to a timedelta.

    Duration-only by design — cron expressions are deferred to the workflow `on: cron(...)`
    work. Raises ValueError on anything else."""
    match = _INTERVAL_RE.fullmatch(spec.strip())
    if not match:
        raise ValueError(f"invalid poll interval {spec!r} (expected e.g. '30s', '5m', '1h', '2d')")
    value, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(value)})


@dataclass
class _Poll:
    name: TriggerId
    interval: timedelta
    poll_fn: PollFn


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

    @property
    def poll_names(self) -> list[TriggerId]:
        return [p.name for p in self._polls]

    def register(self, name: TriggerId, interval: timedelta, poll_fn: PollFn) -> None:
        self._polls.append(_Poll(name, interval, poll_fn))

    async def start(self) -> None:
        """Run every registered poll loop concurrently until cancelled."""
        if not self._polls:
            return
        await asyncio.gather(*(self._run_loop(poll) for poll in self._polls))

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
