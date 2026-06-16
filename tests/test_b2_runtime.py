"""B2 runtime: event fan-out to multiple on: consumers, and recorded run history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness


def _step(step_id: str, event: str) -> dict:
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


def _runtime(manifest: Manifest) -> InMemoryRuntime:
    return InMemoryRuntime(
        manifest,
        harness=StubAgentHarness(),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


def _ping() -> Event:
    return Event(name="Ping", fields={}, id="t", emitted_at=datetime.now(UTC))


def test_event_fans_out_to_all_subscribed_workflows():
    # Two workflows both trigger on Ping — BOTH must run, not just the first.
    manifest = Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Ping": {"fields": {}}}},
            "workflows": {
                "a": {"entry": "x", "steps": {"x": _step("a/x", "Ping")}},
                "b": {"entry": "y", "steps": {"y": _step("b/y", "Ping")}},
            },
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
    rt = _runtime(manifest)
    asyncio.run(rt.trigger(_ping()))
    assert set(rt.execution_log) == {"a/x", "b/y"}


def test_run_history_is_recorded():
    manifest = Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Ping": {"fields": {}}}},
            "workflows": {"a": {"entry": "x", "steps": {"x": _step("a/x", "Ping")}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
    rt = _runtime(manifest)
    run_id = asyncio.run(rt.trigger(_ping()))
    kinds = [e.kind for e in asyncio.run(rt.state.history(run_id))]
    assert kinds == ["run_started", "step_completed", "run_completed"]


def test_unbounded_loop_hits_iteration_cap_not_stack_overflow():
    # A workflow that emits the very event it triggers on loops forever; the iterative
    # drain bounds it with a clear error (no RecursionError / stack overflow).
    loop_step = {
        "id": "loop/tick",
        "trigger": {"kind": "event", "event": "Tick"},
        "after": [],
        "agent": None,
        "output": {},
        "emits": ["Tick"],
        "budget": None,
        "body": "spin",
        "refs": [],
    }
    manifest = Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Tick": {"fields": {}}}},
            "workflows": {"loop": {"entry": "tick", "steps": {"tick": loop_step}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
    rt = InMemoryRuntime(
        manifest,
        harness=StubAgentHarness(),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        max_iterations=5,
    )
    with pytest.raises(RuntimeError, match="max_iterations"):
        asyncio.run(rt.trigger(Event(name="Tick", fields={}, id="t", emitted_at=datetime.now(UTC))))
