"""B3 ingress: the EventReceiver is a validation gate (Stage 1) decoupled from
execution (Stage 2).

Stage 1 — `receive` re-validates every event against the registry before it reaches the
bus (the SensorRunner is untrusted). Stage 2 — `receive` publishes-and-acks; it does NOT
run the workflow. The Runtime drains separately (`drain()` once, or `serve()` in a loop).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, StepOutput, StepResult
from loopy_runtime.manifest_model import EventContract, Manifest
from loopy_runtime.receiver import LocalEventReceiver
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.sensors.runner import FastAPISensorRunner
from loopy_runtime.validation import EventValidationError, validate_event
from tests.stub_harness import StubAgentHarness


def _event(name: str, fields: dict) -> Event:
    return Event(name=name, fields=fields, id="e", emitted_at=datetime.now(UTC))


CONTRACT = {
    "Thing": EventContract(
        fields={
            "count": {"type": "integer"},
            "kind": {"type": "string", "enum": ["a", "b"]},
        }
    )
}


# ── Stage 1: validation ──────────────────────────────────────────────────────────
def test_valid_event_passes():
    validate_event(_event("Thing", {"count": 3, "kind": "a"}), CONTRACT)  # no raise


def test_unknown_event_rejected():
    with pytest.raises(EventValidationError, match="unknown event"):
        validate_event(_event("Nope", {}), CONTRACT)


def test_missing_field_rejected():
    with pytest.raises(EventValidationError, match="missing required"):
        validate_event(_event("Thing", {"count": 1}), CONTRACT)


def test_wrong_type_rejected():
    with pytest.raises(EventValidationError, match="expected integer"):
        validate_event(_event("Thing", {"count": "three", "kind": "a"}), CONTRACT)


def test_bool_is_not_an_integer():
    with pytest.raises(EventValidationError, match="expected integer"):
        validate_event(_event("Thing", {"count": True, "kind": "a"}), CONTRACT)


def test_bad_enum_rejected():
    with pytest.raises(EventValidationError, match="not one of"):
        validate_event(_event("Thing", {"count": 1, "kind": "z"}), CONTRACT)


def test_extra_fields_allowed():
    validate_event(_event("Thing", {"count": 1, "kind": "a", "x": 9}), CONTRACT)  # no raise


def test_receiver_rejects_invalid_event_and_does_not_publish():
    bus = InProcessEventBus()
    seen: list[str] = []

    async def rec(e: Event) -> None:
        seen.append(e.name)

    bus.subscribe("Thing", rec)
    receiver = LocalEventReceiver(bus, CONTRACT)
    with pytest.raises(EventValidationError):
        asyncio.run(receiver.receive(_event("Thing", {"count": "bad", "kind": "a"})))
    assert seen == []  # rejected at the gate — nothing reached the bus


def test_receiver_publishes_valid_event_and_acks():
    bus = InProcessEventBus()
    seen: list[str] = []

    async def rec(e: Event) -> None:
        seen.append(e.name)

    bus.subscribe("Thing", rec)
    receiver = LocalEventReceiver(bus, CONTRACT)
    ack = asyncio.run(receiver.receive(_event("Thing", {"count": 1, "kind": "a"})))
    assert ack is None  # publish-and-ack, not a RunId
    assert seen == ["Thing"]


# ── Stage 2: decoupling ──────────────────────────────────────────────────────────
def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema_version": "1",
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


def _runtime(m: Manifest, bus: InProcessEventBus) -> InMemoryRuntime:
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )


def test_receive_does_not_run_synchronously_until_drained():
    m = _manifest()
    bus = InProcessEventBus()
    rt = _runtime(m, bus)
    receiver = LocalEventReceiver(bus, m.registry.events)

    async def go():
        await receiver.receive(_event("Thing", {}))
        assert rt.execution_log == []  # publish-and-ack: not run yet
        await rt.drain()
        assert rt.execution_log == ["w/s"]  # drained now

    asyncio.run(go())


def test_serve_drains_in_the_background():
    m = _manifest()
    bus = InProcessEventBus()
    rt = _runtime(m, bus)
    receiver = LocalEventReceiver(bus, m.registry.events)

    async def go():
        consumer = asyncio.create_task(rt.serve())
        await receiver.receive(_event("Thing", {}))
        for _ in range(100):  # let the background consumer pick it up
            if rt.execution_log:
                break
            await asyncio.sleep(0.001)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        assert rt.execution_log == ["w/s"]
        assert rt.drain_errors == []

    asyncio.run(go())


# ── #1: run-failure handling — a failed run is recorded, isolated, not raised ─────
class FailingHarness:
    """Harness that raises on a chosen step id; succeeds (empty output) otherwise."""

    def __init__(self, fail_step_id: str):
        self.fail_step_id = fail_step_id

    def required_keys(self, agent):
        return set()

    async def run(self, step, ctx, sandbox):
        if step.id == self.fail_step_id:
            raise RuntimeError("boom")
        return StepResult(output=StepOutput({}), emits={ev: {} for ev in step.emits})


def _wstep(step_id: str, event: str) -> dict:
    return {
        "id": step_id,
        "trigger": {"kind": "event", "event": event},
        "after": [],
        "agent": None,
        "output": {},
        "emits": [],
        "budget": None,
        "body": "do",
        "refs": [],
    }


def _failing_runtime(m: Manifest, bus: InProcessEventBus, fail_step_id: str) -> InMemoryRuntime:
    return InMemoryRuntime(
        m,
        harness=FailingHarness(fail_step_id),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )


def test_failed_run_is_recorded_not_raised():
    m = _manifest()
    bus = InProcessEventBus()
    rt = _failing_runtime(m, bus, "w/s")

    run_id = asyncio.run(rt.trigger(_event("Thing", {})))  # must NOT raise
    assert run_id is not None

    status = asyncio.run(rt.status(run_id))  # status is queryable (no KeyError)
    assert status.state == "failed"
    assert "boom" in (status.error or "")
    assert [s.run_id for s in rt.failed_runs] == [run_id]

    kinds = [e.kind for e in asyncio.run(rt.state.history(run_id))]
    assert kinds == ["run_started", "run_failed"]


def test_failed_run_does_not_strand_siblings():
    # One event fans out to two workflows; the first fails, the second must still run.
    m = Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Thing": {"fields": {}}}},
            "workflows": {
                "boom": {"entry": "s", "steps": {"s": _wstep("boom/s", "Thing")}},
                "ok": {"entry": "s", "steps": {"s": _wstep("ok/s", "Thing")}},
            },
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
    bus = InProcessEventBus()
    rt = _failing_runtime(m, bus, "boom/s")

    asyncio.run(rt.trigger(_event("Thing", {})))
    assert "ok/s" in rt.execution_log  # sibling ran despite the other run failing
    assert [s.run_id for s in rt.failed_runs] == ["boom-1"]


def test_serve_survives_a_failing_run():
    m = _manifest()
    bus = InProcessEventBus()
    rt = _failing_runtime(m, bus, "w/s")
    receiver = LocalEventReceiver(bus, m.registry.events)

    async def go():
        consumer = asyncio.create_task(rt.serve())
        await receiver.receive(_event("Thing", {}))
        for _ in range(100):
            if rt.failed_runs:
                break
            await asyncio.sleep(0.001)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        assert rt.failed_runs and rt.failed_runs[0].state == "failed"
        assert rt.drain_errors == []  # a per-run failure is not a drain-level fault

    asyncio.run(go())


# ── #3: a validation failure becomes HTTP 422 at the webhook, not a 500 ───────────
def test_webhook_returns_422_on_invalid_event():
    bus = InProcessEventBus()
    host = FastAPISensorRunner(LocalEventReceiver(bus, CONTRACT))
    # the sensor produces a Thing missing its required 'kind' field
    host.register_webhook("/hooks/x", lambda payload: _event("Thing", {"count": 1}))
    route = next(r for r in host.app.routes if getattr(r, "path", None) == "/hooks/x")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(route.endpoint({}))
    assert excinfo.value.status_code == 422
    assert "kind" in str(excinfo.value.detail)
