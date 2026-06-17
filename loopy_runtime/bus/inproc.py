"""In-process EventBus (B5) — async pub/sub on the asyncio loop, no broker.

`publish` is async and `Event` is serializable, so a `RedisEventBus`/`NatsEventBus`
is a drop-in behind the same Protocol (the code swap is trivial; reliable redelivery
is part of the later durability work).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable

from loopy_runtime.contract import Event, EventName


class InProcessEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[EventName, list[Callable[[Event], Awaitable[None]]]] = defaultdict(
            list
        )

    def subscribe(self, name: EventName, handler: Callable[[Event], Awaitable[None]]) -> None:
        self._subscribers[name].append(handler)

    async def publish(self, event: Event) -> None:
        for handler in self._subscribers.get(event.name, []):
            await handler(event)

    async def run(self) -> None:
        """No-op: in-process delivery happens inline in `publish`; there is no broker to
        consume from. Present so the in-proc bus satisfies the same lifecycle as a networked
        bus (the server starts `run()` as a task regardless of which bus is wired in)."""
        return
