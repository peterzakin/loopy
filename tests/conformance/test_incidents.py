"""Conformance suite (§3.3): run the incidents reference manifest end-to-end and
assert step outputs, emitted events, and order — incl. the cross-workflow cascade.

Driven by the StubAgentHarness so it's offline and deterministic. Every future
backend (DurableLite, Temporal, ...) must pass this same suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import load_manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "incidents.manifest.json"


def _runtime():
    m = load_manifest(GOLDEN)
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


def _incident() -> Event:
    return Event(
        name="Incident",
        fields={
            "source": "sentry",
            "issue_id": "ISS-1",
            "title": "boom",
            "link": "https://example.test/i/1",
        },
        id="trigger",
        emitted_at=datetime.now(UTC),
    )


def test_incident_triggers_full_cascade_in_order():
    rt = _runtime()
    run_id = asyncio.run(rt.trigger(_incident()))
    assert run_id is not None
    # triage runs, emits WorkItem -> resolve cascade runs to completion.
    assert rt.execution_log == [
        "triage/investigate",
        "resolve/arbitrate",
        "resolve/fix",
        "resolve/review",
        "resolve/ship",
    ]
    # WorkItem emitted by investigate; GoalShipped emitted by ship (terminal).
    assert rt.emitted_log == ["WorkItem", "GoalShipped"]


def test_unsubscribed_event_starts_no_run():
    rt = _runtime()
    run_id = asyncio.run(
        rt.trigger(Event(name="Nope", fields={}, id="x", emitted_at=datetime.now(UTC)))
    )
    assert run_id is None
    assert rt.execution_log == []
