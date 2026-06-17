"""EventReceiver implementations — the executor-side intake for events.

A `SensorRunner` (any language/process) produces an `Event` and hands it to an
`EventReceiver`, which injects it into the runtime. In-process that's a direct
`Runtime.trigger` call; across a process/language boundary it's an HTTP endpoint or
a broker consumer (future) that ultimately calls a local receiver. This seam is what
lets a non-Python `SensorRunner` feed the single Python executor.
"""

from __future__ import annotations

from loopy_runtime.contract import Event, RunId


class LocalEventReceiver:
    """In-process receiver: hand the event straight to the runtime (publish + drain)."""

    def __init__(self, runtime):
        self._runtime = runtime

    async def receive(self, event: Event) -> RunId | None:
        return await self._runtime.trigger(event)
