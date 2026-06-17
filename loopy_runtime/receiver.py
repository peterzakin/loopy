"""EventReceiver — the trusted ingress gate in front of the EventBus.

A `SensorRunner` (untrusted; any language/transport) produces an `Event` and hands it
to the `EventReceiver`. The receiver re-validates the event against the registry
contract — the producer is not trusted to — and publishes it to the `EventBus`; the
`Runtime` consumes off the bus and drives the run.

`receive` is **publish-and-acknowledge**: it does NOT run the workflow (a remote
receiver could not hold a connection open for a minutes-to-days run), so it returns once
the event is accepted and on the bus, not when the run completes. It holds only the
registry contract (to validate) and an `EventBus` handle (to publish) — nothing else —
so it can later run as its own service in front of a networked broker.
"""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.contract import Event, EventBus, RunId
from loopy_runtime.manifest_model import EventContract
from loopy_runtime.validation import validate_event


class LocalEventReceiver:
    """In-process receiver: validate against the registry, then publish to the bus."""

    def __init__(self, bus: EventBus, events: Mapping[str, EventContract]):
        self._bus = bus
        self._events = events

    async def receive(self, event: Event) -> RunId | None:
        validate_event(event, self._events)  # untrusted producer → re-validate at the gate
        await self._bus.publish(event)  # the Runtime consumes off the bus and runs it
        return None  # publish-and-ack: accepted, not yet run — never a RunId
