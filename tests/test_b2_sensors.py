"""B2 SensorHost: dispatch hands events to the sink; webhook -> trigger -> cascade.

Exercised without an HTTP server (the route handler is invoked directly), so no
httpx/uvicorn needed in CI.
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
from loopy_runtime.sensors.host import FastAPISensorHost
from loopy_runtime.sensors.host import synthesizing_publisher as _synthesizing_publisher
from tests.stub_harness import StubAgentHarness

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def _event(name="X"):
    return Event(name=name, fields={}, id="e", emitted_at=datetime.now(UTC))


def test_register_webhook_adds_route():
    async def sink(event):
        return None

    host = FastAPISensorHost(sink)
    host.register_webhook("/hooks/x", lambda payload: None)
    assert "/hooks/x" in [r.path for r in host.app.routes]
    assert host.webhook_paths == ["/hooks/x"]


def test_dispatch_hands_returned_event_to_sink():
    seen: list[str] = []

    async def sink(event):
        seen.append(event.name)

    host = FastAPISensorHost(sink)
    result = asyncio.run(host._dispatch(lambda payload: _event("X"), {}))
    assert result == {"emitted": "X"}
    assert seen == ["X"]


def test_dispatch_skips_when_sensor_returns_none():
    seen: list[str] = []

    async def sink(event):
        seen.append(event.name)

    host = FastAPISensorHost(sink)
    result = asyncio.run(host._dispatch(lambda payload: None, {}))
    assert result == {"emitted": None}
    assert seen == []


def test_webhook_drives_full_cascade():
    # A webhook publishing Incident should trigger triage -> resolve, end to end.
    m = load_manifest(GOLDEN)
    runtime = InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    host = FastAPISensorHost(runtime.trigger)
    sentry = next(s for s in m.sensors if s.emits == "Incident")
    host.register_webhook(sentry.trigger.path, _synthesizing_publisher(m, sentry))

    asyncio.run(host._dispatch(_synthesizing_publisher(m, sentry), {"issue_id": "ISS-7"}))

    assert runtime.execution_log[0] == "triage/investigate"
    assert "resolve/ship" in runtime.execution_log
    assert runtime.emitted_log == ["WorkItem", "GoalShipped"]
