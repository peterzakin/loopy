"""B7 (in-process) poll scheduler: tick -> poll -> deliver -> advance watermark -> sleep.

All deterministic — an injected clock and a fake `sleep` that stops the loop after a set
number of iterations, so nothing sleeps for real. Mirrors the receiver/state patterns in
test_b3_ingress.py / test_b2_sensors.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, Tick
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.receiver import LocalEventReceiver
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.sensors.scheduler import PollScheduler, _Poll, parse_interval
from loopy_runtime.state.inmemory import InMemoryStateStore
from tests.stub_harness import StubAgentHarness

T0 = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def _event(name: str = "DepAlert") -> Event:
    return Event(name=name, fields={}, id="e", emitted_at=datetime.now(UTC))


class RecordingReceiver:
    """Test EventReceiver: records the events delivered to it (the validation gate stand-in)."""

    def __init__(self) -> None:
        self.seen: list[Event] = []

    async def receive(self, event: Event):
        self.seen.append(event)
        return None


class _Stop(Exception):
    """Raised by the fake sleep to break the otherwise-infinite poll loop in a test."""


class FakeClock:
    """A clock the test advances explicitly (the fake sleep advances it by the slept span)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class StoppingSleep:
    """Fake `sleep`: advances the clock by the slept span, then raises `_Stop` after
    `stop_after` calls so a `_run_loop` runs a bounded number of ticks without real time."""

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


def _scheduler(receiver, state, clock=None, sleep=None) -> PollScheduler:
    return PollScheduler(receiver=receiver, state=state, clock=clock, sleep=sleep)


# ── fan-out: one poll fn returning N events -> N deliveries through the receiver ──────────
def test_one_tick_fans_out_n_events_to_the_receiver():
    receiver = RecordingReceiver()
    state = InMemoryStateStore()
    sched = _scheduler(receiver, state, clock=lambda: T0)
    poll = _Poll("scan", MIN, lambda tick: [_event(), _event(), _event()])

    asyncio.run(sched._tick_once(poll))

    assert len(receiver.seen) == 3  # N events -> N runs via the receiver
    assert all(e.name == "DepAlert" for e in receiver.seen)


def test_tick_with_no_events_delivers_nothing_but_still_advances():
    receiver = RecordingReceiver()
    state = InMemoryStateStore()
    sched = _scheduler(receiver, state, clock=lambda: T0)
    poll = _Poll("scan", MIN, lambda tick: [])

    asyncio.run(sched._tick_once(poll))

    assert receiver.seen == []
    assert asyncio.run(state.get_watermark("scan")) == T0  # an empty window is still progress


# ── watermark: advances on success, holds on failure ────────────────────────────────────
def test_watermark_advances_on_success():
    state = InMemoryStateStore()
    sched = _scheduler(RecordingReceiver(), state, clock=lambda: T0)
    poll = _Poll("scan", MIN, lambda tick: [_event()])

    assert asyncio.run(state.get_watermark("scan")) is None
    asyncio.run(sched._tick_once(poll))
    assert asyncio.run(state.get_watermark("scan")) == T0  # advanced to scheduled_at


def test_watermark_holds_when_poll_fn_raises():
    state = InMemoryStateStore()
    asyncio.run(state.set_watermark("scan", T0))  # a prior successful tick
    receiver = RecordingReceiver()
    later = T0 + MIN
    sched = _scheduler(receiver, state, clock=lambda: later)

    def boom(tick: Tick):
        raise RuntimeError("upstream down")

    poll = _Poll("scan", MIN, boom)

    with pytest.raises(RuntimeError, match="upstream down"):
        asyncio.run(sched._tick_once(poll))

    assert asyncio.run(state.get_watermark("scan")) == T0  # held — not advanced to `later`
    assert receiver.seen == []


def test_failure_does_not_advance_so_next_tick_retries_same_window():
    # poll fn fails once, then succeeds: the retry sees the SAME last_run (the held watermark).
    state = InMemoryStateStore()
    asyncio.run(state.set_watermark("scan", T0))
    clock = FakeClock(T0 + MIN)
    sleep = StoppingSleep(clock, stop_after=2)  # two ticks, then stop
    sched = _scheduler(RecordingReceiver(), state, clock=clock, sleep=sleep)

    windows: list[datetime | None] = []
    calls = {"n": 0}

    def poll_fn(tick: Tick):
        windows.append(tick.last_run)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return [_event()]

    with pytest.raises(_Stop):
        asyncio.run(sched._run_loop(_Poll("scan", MIN, poll_fn)))

    # First tick failed (window started at T0); second tick retried the SAME window start.
    assert windows == [T0, T0]


# ── cold start: first tick scans exactly one window (scheduled_at - interval) ─────────────
def test_cold_start_window_is_one_interval_back():
    state = InMemoryStateStore()  # no watermark yet
    captured: list[Tick] = []
    sched = _scheduler(RecordingReceiver(), state, clock=lambda: T0)
    poll = _Poll("scan", MIN, lambda tick: captured.append(tick) or [])

    asyncio.run(sched._tick_once(poll))

    assert captured[0].scheduled_at == T0
    assert captured[0].last_run == T0 - MIN  # one window back, not None / not all history


def test_second_tick_uses_previous_scheduled_at_as_last_run():
    state = InMemoryStateStore()
    clock = FakeClock(T0)
    sleep = StoppingSleep(clock, stop_after=2)
    sched = _scheduler(RecordingReceiver(), state, clock=clock, sleep=sleep)

    captured: list[Tick] = []
    poll = _Poll("scan", MIN, lambda tick: captured.append(tick) or [_event()])

    with pytest.raises(_Stop):
        asyncio.run(sched._run_loop(poll))

    assert captured[0].last_run == T0 - MIN  # cold start
    assert captured[1].last_run == T0  # the first tick's scheduled_at became the watermark
    assert captured[1].scheduled_at == T0 + MIN  # clock advanced by the slept interval


# ── sequential / no overlap: next tick only after the current delivery completes ──────────
def test_sequential_no_overlap_within_a_sensor():
    state = InMemoryStateStore()
    clock = FakeClock(T0)
    sleep = StoppingSleep(clock, stop_after=2)  # two ticks
    receiver = RecordingReceiver()
    sched = _scheduler(receiver, state, clock=clock, sleep=sleep)

    order: list[str] = []

    class SlowReceiver:
        async def receive(self, event: Event):
            order.append(f"enter:{event.id}")
            await asyncio.sleep(0)  # yield to the loop — a concurrent tick could interleave here
            order.append(f"exit:{event.id}")
            return None

    sched._receiver = SlowReceiver()
    seq = {"n": 0}

    # Tag events so we can see ordering: each tick delivers two events with distinct ids.
    def tagging_poll(tick: Tick):
        seq["n"] += 1
        return [
            Event(name="DepAlert", fields={}, id=f"t{seq['n']}-a", emitted_at=T0),
            Event(name="DepAlert", fields={}, id=f"t{seq['n']}-b", emitted_at=T0),
        ]

    with pytest.raises(_Stop):
        asyncio.run(sched._run_loop(_Poll("scan", MIN, tagging_poll)))

    # Strictly non-overlapping: every enter is immediately followed by its own exit, and a
    # tick's two events are delivered fully before the next tick's events begin.
    assert order == [
        "enter:t1-a", "exit:t1-a", "enter:t1-b", "exit:t1-b",
        "enter:t2-a", "exit:t2-a", "enter:t2-b", "exit:t2-b",
    ]


def test_run_loop_survives_a_failing_tick_and_keeps_going():
    state = InMemoryStateStore()
    clock = FakeClock(T0)
    sleep = StoppingSleep(clock, stop_after=3)
    receiver = RecordingReceiver()
    sched = _scheduler(receiver, state, clock=clock, sleep=sleep)

    calls = {"n": 0}

    def flaky(tick: Tick):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return [_event()]

    with pytest.raises(_Stop):
        asyncio.run(sched._run_loop(_Poll("scan", MIN, flaky)))

    assert calls["n"] == 3  # loop kept ticking past the first failure
    assert len(receiver.seen) == 2  # ticks 2 and 3 delivered; tick 1 failed


# ── start(): one task per sensor, independent watermarks ─────────────────────────────────
def test_start_runs_one_loop_per_sensor():
    state = InMemoryStateStore()
    clock = FakeClock(T0)
    sleep = StoppingSleep(clock, stop_after=1)  # stop each loop on its first sleep
    receiver = RecordingReceiver()
    sched = _scheduler(receiver, state, clock=clock, sleep=sleep)
    sched.register("a", MIN, lambda tick: [_event("A")])
    sched.register("b", MIN, lambda tick: [_event("B")])
    assert sched.poll_names == ["a", "b"]

    # gather() surfaces the first _Stop; both loops have ticked once by then.
    with pytest.raises(_Stop):
        asyncio.run(sched.start())

    delivered = {e.name for e in receiver.seen}
    assert delivered == {"A", "B"}
    assert asyncio.run(state.get_watermark("a")) == T0
    assert asyncio.run(state.get_watermark("b")) == T0


def test_start_with_no_polls_is_a_noop():
    sched = _scheduler(RecordingReceiver(), InMemoryStateStore())
    asyncio.run(sched.start())  # returns immediately, no tasks


# ── integration: a poll tick drives a real run through receiver -> bus -> serve() ─────────
def _poll_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema_version": "2",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Thing": {"fields": {}}}},
            "workflows": {
                "w": {
                    "entry": "s",
                    "steps": {
                        "s": {
                            "id": "w/s",
                            "trigger": {"kind": "event", "event": "Thing"},
                            "after": [],
                            "agent": None,
                            "output": {},
                            "emits": [],
                            "budget": None,
                            "body": "do",
                            "refs": [],
                        }
                    },
                }
            },
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def test_poll_events_flow_through_the_real_delivery_path():
    # The scheduler hands events to the SAME LocalEventReceiver gate webhooks use; the
    # runtime consumes off the bus on serve() and runs the workflow. No path changes.
    m = _poll_manifest()
    bus = InProcessEventBus()
    runtime = InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )
    receiver = LocalEventReceiver(bus, m.registry.events)
    # share the runtime's StateStore for watermarks, exactly as `loopy run` wires it
    sched = PollScheduler(receiver=receiver, state=runtime.state, clock=lambda: T0)
    sched.register("ping", MIN, lambda tick: [_event("Thing"), _event("Thing")])

    async def go():
        consumer = asyncio.create_task(runtime.serve())
        await sched._tick_once(sched._polls[0])  # one tick, two events
        for _ in range(100):  # let the background consumer drain both
            if len(runtime.execution_log) >= 2:
                break
            await asyncio.sleep(0.001)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    asyncio.run(go())
    assert runtime.execution_log == ["w/s", "w/s"]  # two fanned-out events -> two runs
    assert asyncio.run(runtime.state.get_watermark("ping")) == T0


def test_poll_event_failing_the_validation_gate_holds_the_watermark():
    # An event the registry rejects raises out of the receiver -> _tick_once -> watermark held.
    m = _poll_manifest()
    bus = InProcessEventBus()
    receiver = LocalEventReceiver(bus, m.registry.events)
    state = InMemoryStateStore()
    sched = PollScheduler(receiver=receiver, state=state, clock=lambda: T0)
    poll = _Poll("ping", MIN, lambda tick: [_event("Unregistered")])

    with pytest.raises(Exception):  # noqa: B017 - EventValidationError from the gate
        asyncio.run(sched._tick_once(poll))
    assert asyncio.run(state.get_watermark("ping")) is None  # never advanced


# ── interval parsing ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
        (" 5m ", timedelta(minutes=5)),
    ],
)
def test_parse_interval_durations(spec, expected):
    assert parse_interval(spec) == expected


@pytest.mark.parametrize("spec", ["", "5", "m", "5x", "* * * * *", "5 m", "-1m"])
def test_parse_interval_rejects_non_durations(spec):
    with pytest.raises(ValueError, match="invalid poll interval"):
        parse_interval(spec)
