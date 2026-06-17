"""B2 SensorRunner: dispatch hands events to the sink; webhook -> trigger -> cascade.

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
from loopy_runtime.receiver import LocalEventReceiver
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from loopy_runtime.sensors.runner import FastAPISensorRunner
from loopy_runtime.sensors.runner import synthesizing_publisher as _synthesizing_publisher
from tests.stub_harness import StubAgentHarness

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def _event(name="X"):
    return Event(name=name, fields={}, id="e", emitted_at=datetime.now(UTC))


class RecordingReceiver:
    """Test EventReceiver: records the names of events delivered to it."""

    def __init__(self):
        self.seen: list[str] = []

    async def receive(self, event):
        self.seen.append(event.name)
        return None


def test_register_webhook_adds_route():
    host = FastAPISensorRunner(RecordingReceiver())
    host.register_webhook("/hooks/x", lambda payload: None)
    assert "/hooks/x" in [r.path for r in host.app.routes]
    assert host.webhook_paths == ["/hooks/x"]


def test_dispatch_hands_returned_event_to_receiver():
    receiver = RecordingReceiver()
    host = FastAPISensorRunner(receiver)
    result = asyncio.run(host._dispatch(lambda payload: _event("X"), {}))
    assert result == {"emitted": "X"}
    assert receiver.seen == ["X"]


def test_dispatch_skips_when_sensor_returns_none():
    receiver = RecordingReceiver()
    host = FastAPISensorRunner(receiver)
    result = asyncio.run(host._dispatch(lambda payload: None, {}))
    assert result == {"emitted": None}
    assert receiver.seen == []


def test_webhook_drives_full_cascade():
    # A webhook publishing Incident should trigger triage -> resolve, end to end.
    # receive() now publishes-and-acks, so drain explicitly (serve() does this in the server).
    m = load_manifest(GOLDEN)
    bus = InProcessEventBus()
    runtime = InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )
    host = FastAPISensorRunner(LocalEventReceiver(bus, m.registry.events))
    sentry = next(s for s in m.sensors if s.emits == "Incident")
    host.register_webhook(sentry.trigger.path, _synthesizing_publisher(m, sentry))

    async def go():
        await host._dispatch(_synthesizing_publisher(m, sentry), {"issue_id": "ISS-7"})
        await runtime.drain()

    asyncio.run(go())

    assert runtime.execution_log[0] == "triage/investigate"
    assert "resolve/ship" in runtime.execution_log
    assert runtime.emitted_log == ["WorkItem", "GoalShipped"]


def test_local_event_receiver_validates_publishes_then_runtime_consumes():
    # The in-proc EventReceiver validates against the registry and publishes to the bus;
    # the Runtime consumes off the bus when drained (publish-and-ack, not run-on-receive).
    m = load_manifest(GOLDEN)
    bus = InProcessEventBus()
    runtime = InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=bus,
    )
    receiver = LocalEventReceiver(bus, m.registry.events)
    incident = Event(
        name="Incident",
        fields={
            "source": "sentry",
            "issue_id": "ISS-1",
            "title": "boom",
            "link": "https://example.test/i/1",
        },
        id="e",
        emitted_at=datetime.now(UTC),
    )

    async def go():
        assert await receiver.receive(incident) is None  # publish-and-ack
        await runtime.drain()

    asyncio.run(go())
    assert runtime.execution_log[0] == "triage/investigate"
