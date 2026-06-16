"""B2 SensorHost: dispatch publishes returned events; webhook -> bus -> run cascade.

Exercised without an HTTP server (the route handler is invoked directly), so no
httpx/uvicorn needed in CI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.cli import _synthesizing_publisher
from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import load_manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.sensors.host import FastAPISensorHost
from tests.stub_harness import StubAgentHarness

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def _event(name="X"):
    return Event(name=name, fields={}, id="e", emitted_at=datetime.now(UTC))


def test_register_webhook_adds_route():
    host = FastAPISensorHost(InProcessEventBus())
    host.register_webhook("/hooks/x", lambda payload: None)
    assert "/hooks/x" in [r.path for r in host.app.routes]
    assert host.webhook_paths == ["/hooks/x"]


def test_dispatch_publishes_returned_event():
    bus = InProcessEventBus()
    received: list[str] = []
    bus.subscribe("X", lambda ev: _collect(received, ev))
    host = FastAPISensorHost(bus)
    result = asyncio.run(host._dispatch(lambda payload: _event("X"), {}))
    assert result == {"emitted": "X"}
    assert received == ["X"]


def test_dispatch_skips_when_sensor_returns_none():
    bus = InProcessEventBus()
    received: list[str] = []
    bus.subscribe("X", lambda ev: _collect(received, ev))
    host = FastAPISensorHost(bus)
    result = asyncio.run(host._dispatch(lambda payload: None, {}))
    assert result == {"emitted": None}
    assert received == []


def test_webhook_drives_full_cascade():
    # A webhook publishing Incident should trigger triage -> resolve, end to end.
    m = load_manifest(GOLDEN)
    bus = InProcessEventBus()
    runtime = InMemoryRuntime(
        m,
        harness=StubAgentHarness(),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )
    host = FastAPISensorHost(bus)
    sentry = next(s for s in m.sensors if s.emits == "Incident")
    host.register_webhook(sentry.trigger.path, _synthesizing_publisher(m, sentry))

    asyncio.run(host._dispatch(_synthesizing_publisher(m, sentry), {"issue_id": "ISS-7"}))

    assert runtime.execution_log[0] == "triage/investigate"
    assert "resolve/ship" in runtime.execution_log
    assert runtime.emitted_log == ["WorkItem", "GoalShipped"]


async def _collect(sink: list, event: Event) -> None:
    sink.append(event.name)
