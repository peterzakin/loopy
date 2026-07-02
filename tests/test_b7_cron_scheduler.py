"""B7 (in-process) cron triggers: `on: cron(...)` entry steps fire on a schedule.

The cron analogue of test_b7_poll_scheduler.py. Two layers:
- `cron_next`/`cron_prev` math (incl. tz) — pure, no clock.
- `PollScheduler._cron_run_loop` timing + `InMemoryRuntime.tick` run instantiation — driven by
  the same injected FakeClock / StoppingSleep so nothing sleeps for real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.sensors.scheduler import PollScheduler, cron_next, cron_prev
from loopy_runtime.state.inmemory import InMemoryStateStore
from tests.stub_harness import StubAgentHarness

# 2026-06-18 is a Thursday; a daily "0 3 * * *" cron has its next fire at 03:00 the 19th.
NOON = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
DAILY_3AM = "0 3 * * *"


class _Stop(Exception):
    """Raised by the fake sleep to break the otherwise-infinite cron loop in a test."""


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class StoppingSleep:
    """Fake sleep: advances the clock by the slept span, then raises `_Stop` after `stop_after`
    calls. With the sleep-first cron loop, N fires need stop_after = N + 1."""

    def __init__(self, clock: FakeClock, stop_after: int) -> None:
        self.clock = clock
        self.stop_after = stop_after
        self.calls = 0
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        self.slept.append(seconds)
        if self.calls >= self.stop_after:
            raise _Stop
        self.clock.advance(seconds)


# ── cron math ─────────────────────────────────────────────────────────────────────────────
def test_cron_next_is_strictly_after_and_utc():
    nxt = cron_next(DAILY_3AM, NOON)
    assert nxt == datetime(2026, 6, 19, 3, 0, tzinfo=UTC)  # next 3am, the following day
    # strictly-after: from exactly 03:00 we get the *next* day's 03:00, never the same instant
    assert cron_next(DAILY_3AM, datetime(2026, 6, 19, 3, 0, tzinfo=UTC)) == datetime(
        2026, 6, 20, 3, 0, tzinfo=UTC
    )


def test_cron_prev_is_the_cold_start_window():
    prev = cron_prev(DAILY_3AM, NOON)
    assert prev == datetime(2026, 6, 18, 3, 0, tzinfo=UTC)  # this morning's 3am


def test_cron_tz_is_interpreted_in_the_zone_then_returned_utc():
    # 03:00 America/New_York on 2026-06-18 is EDT (UTC-4) -> 07:00 UTC.
    nxt = cron_next(DAILY_3AM, datetime(2026, 6, 18, 0, 0, tzinfo=UTC), tz="America/New_York")
    assert nxt == datetime(2026, 6, 18, 7, 0, tzinfo=UTC)


# ── runtime.tick: builds the tick event, runs the entry, advances the watermark ────────────
def _cron_manifest(*, emits: list[str] | None = None) -> Manifest:
    events = {"WorkItem": {"fields": {}}} if emits else {}
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": events},
            "workflows": {
                "upkeep": {
                    "entry": "scan",
                    "steps": {
                        "scan": {
                            "id": "upkeep/scan",
                            "trigger": {"kind": "cron", "expr": DAILY_3AM, "tz": None},
                            "after": [],
                            "agent": None,
                            "output": {},
                            "emits": emits or [],
                            "budget": None,
                            "body": "scan since {{ event.last_run }}",
                            "refs": [
                                {
                                    "producer": "event",
                                    "field": "last_run",
                                    "raw": "{{ event.last_run }}",
                                }
                            ],
                        }
                    },
                }
            },
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _runtime(m: Manifest) -> InMemoryRuntime:
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


def test_tick_instantiates_a_run_rooted_at_the_cron_entry():
    runtime = _runtime(_cron_manifest())
    sched_at = datetime(2026, 6, 19, 3, 0, tzinfo=UTC)

    run_id = asyncio.run(runtime.tick("upkeep/scan", sched_at))

    assert run_id is not None
    assert runtime.execution_log == ["upkeep/scan"]  # the entry step ran


def test_tick_event_carries_scheduled_at_and_cold_start_last_run():
    # No watermark yet -> last_run is the previous occurrence (cold-start window), and the
    # rendered body proves the tick event reached the step.
    captured: list = []
    m = _cron_manifest()
    runtime = _runtime(m)

    original = runtime.runner._run_step

    async def spy(step, ctx):  # capture the triggering event the runtime built
        captured.append(ctx.event)
        return await original(step, ctx)

    runtime.runner._run_step = spy  # type: ignore[method-assign]
    sched_at = datetime(2026, 6, 19, 3, 0, tzinfo=UTC)
    asyncio.run(runtime.tick("upkeep/scan", sched_at))

    event = captured[0]
    assert event.fields["scheduled_at"] == sched_at
    assert event.fields["last_run"] == datetime(2026, 6, 18, 3, 0, tzinfo=UTC)  # one window back


def test_tick_advances_the_watermark_to_scheduled_at():
    runtime = _runtime(_cron_manifest())
    sched_at = datetime(2026, 6, 19, 3, 0, tzinfo=UTC)

    assert asyncio.run(runtime.state.get_watermark("upkeep/scan")) is None
    asyncio.run(runtime.tick("upkeep/scan", sched_at))
    assert asyncio.run(runtime.state.get_watermark("upkeep/scan")) == sched_at


def test_second_tick_uses_the_first_ticks_scheduled_at_as_last_run():
    captured: list = []
    runtime = _runtime(_cron_manifest())
    original = runtime.runner._run_step

    async def spy(step, ctx):
        captured.append(ctx.event)
        return await original(step, ctx)

    runtime.runner._run_step = spy  # type: ignore[method-assign]
    first = datetime(2026, 6, 19, 3, 0, tzinfo=UTC)
    second = datetime(2026, 6, 20, 3, 0, tzinfo=UTC)
    asyncio.run(runtime.tick("upkeep/scan", first))
    asyncio.run(runtime.tick("upkeep/scan", second))

    assert captured[1].fields["last_run"] == first  # the prior fire's scheduled_at


def test_tick_for_unknown_trigger_is_a_noop():
    runtime = _runtime(_cron_manifest())
    assert asyncio.run(runtime.tick("does/not-exist", NOON)) is None
    assert runtime.execution_log == []


def test_tick_cascade_emits_to_subscribed_workflows():
    # A cron entry that emits an event the runtime drains in the same _drain pass.
    runtime = _runtime(_cron_manifest(emits=["WorkItem"]))
    sched_at = datetime(2026, 6, 19, 3, 0, tzinfo=UTC)
    asyncio.run(runtime.tick("upkeep/scan", sched_at))
    assert runtime.emitted_log == ["WorkItem"]


# ── scheduler cron loop: sleeps to the occurrence, then fires `runtime.tick` ────────────────
def test_cron_loop_fires_at_each_occurrence():
    fired: list[datetime] = []

    async def fire(scheduled_at: datetime):
        fired.append(scheduled_at)

    clock = FakeClock(NOON)
    sleep = StoppingSleep(clock, stop_after=3)  # two fires, then stop
    sched = PollScheduler(
        receiver=None, state=InMemoryStateStore(), clock=clock, sleep=sleep
    )
    sched.register_cron("upkeep/scan", DAILY_3AM, None, fire)
    assert sched.cron_names == ["upkeep/scan"]

    with pytest.raises(_Stop):
        asyncio.run(sched._cron_run_loop(sched._crons[0]))

    # First fire at the next 3am, second at the following day's 3am.
    assert fired == [
        datetime(2026, 6, 19, 3, 0, tzinfo=UTC),
        datetime(2026, 6, 20, 3, 0, tzinfo=UTC),
    ]


def test_cron_loop_survives_a_failing_fire():
    calls = {"n": 0}

    async def flaky(scheduled_at: datetime):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    clock = FakeClock(NOON)
    sleep = StoppingSleep(clock, stop_after=3)
    sched = PollScheduler(receiver=None, state=InMemoryStateStore(), clock=clock, sleep=sleep)
    sched.register_cron("upkeep/scan", DAILY_3AM, None, flaky)

    with pytest.raises(_Stop):
        asyncio.run(sched._cron_run_loop(sched._crons[0]))

    assert calls["n"] == 2  # kept going past the first failure


def test_start_runs_poll_and_cron_loops_together():
    clock = FakeClock(NOON)
    sleep = StoppingSleep(clock, stop_after=1)  # stop each loop on its first sleep
    sched = PollScheduler(receiver=None, state=InMemoryStateStore(), clock=clock, sleep=sleep)
    fired = {"cron": 0}

    async def fire(_scheduled_at: datetime):
        fired["cron"] += 1

    sched.register_cron("upkeep/scan", DAILY_3AM, None, fire)
    # With stop_after=1 the cron loop stops on its first sleep (before firing) — we only assert
    # the loop is wired into start(), not that it fired.
    with pytest.raises(_Stop):
        asyncio.run(sched.start())


# ── integration: scheduler cron loop drives a real run via runtime.tick ────────────────────
def test_cron_loop_drives_a_real_run_through_tick():
    m = _cron_manifest()
    runtime = _runtime(m)
    clock = FakeClock(NOON)
    sleep = StoppingSleep(clock, stop_after=2)  # one fire, then stop
    sched = PollScheduler(receiver=None, state=runtime.state, clock=clock, sleep=sleep)

    def _fire(scheduled_at, _id="upkeep/scan"):
        return runtime.tick(_id, scheduled_at)

    sched.register_cron("upkeep/scan", DAILY_3AM, None, _fire)

    with pytest.raises(_Stop):
        asyncio.run(sched._cron_run_loop(sched._crons[0]))

    assert runtime.execution_log == ["upkeep/scan"]  # the cron tick instantiated a run
    assert asyncio.run(runtime.state.get_watermark("upkeep/scan")) == datetime(
        2026, 6, 19, 3, 0, tzinfo=UTC
    )
