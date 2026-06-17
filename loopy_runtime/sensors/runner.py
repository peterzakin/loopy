"""SensorRunner (B1 ingress) — webhook server + poll scheduler that injects events.

A registered sensor callable takes the incoming payload and returns an `Event` (or
None to emit nothing); the host hands it to `sink` — the runtime's `trigger`, which
publishes it and drains the resulting cascade. v1 ships the webhook path; poll
registration is recorded but the durable scheduler lands with cron/watermarks
(B7/B8). Executing user-authored sensor *modules* (which import the `loopy` authoring
shim) is handled by `loopy run`'s sensor loader; `synthesizing_publisher` is the
dev fallback when a module can't be loaded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI

from loopy_runtime.contract import Event, EventReceiver
from loopy_runtime.payloads import synthesize_fields

if TYPE_CHECKING:
    from loopy_runtime.manifest_model import Manifest, SensorSpec

# A sensor callable: payload -> Event | None.
SensorFn = Callable[[dict], "Event | None"]


def synthesizing_publisher(manifest: Manifest, sensor: SensorSpec) -> SensorFn:
    """Dev fallback: publish the sensor's declared `emits` event with fields synthesized
    from the contract (merged with the request payload). Used when the real sensor
    module can't be loaded."""
    contract = manifest.registry.events.get(sensor.emits)
    schema = contract.fields if contract else {}
    seq = {"n": 0}

    def publisher(payload: dict) -> Event:
        seq["n"] += 1
        return Event(
            name=sensor.emits,
            fields=synthesize_fields(schema, payload or {}),
            id=f"sensor-{sensor.name}-{seq['n']}",
            emitted_at=datetime.now(UTC),
        )

    return publisher


class FastAPISensorRunner:
    def __init__(self, receiver: EventReceiver) -> None:
        self.receiver = receiver
        self.app = FastAPI()
        self.webhook_paths: list[str] = []
        self.polls: list[tuple[timedelta, SensorFn, str]] = []

    def register_webhook(self, path: str, fn: SensorFn) -> None:
        async def handler(payload: dict | None = None):
            return await self._dispatch(fn, payload or {})

        self.app.add_api_route(path, handler, methods=["POST"])
        self.webhook_paths.append(path)

    def register_poll(self, interval: timedelta, fn: SensorFn, t: str) -> None:
        # Recorded for v1; the durable poll scheduler is deferred (B7/B8).
        self.polls.append((interval, fn, t))

    async def _dispatch(self, fn: SensorFn, payload: dict) -> dict:
        event = fn(payload)
        if event is None:
            return {"emitted": None}
        await self.receiver.receive(event)
        return {"emitted": event.name}

    async def start(self, host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
        import uvicorn

        await uvicorn.Server(uvicorn.Config(self.app, host=host, port=port)).serve()
