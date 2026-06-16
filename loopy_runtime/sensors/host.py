"""SensorHost (B1 ingress) — webhook server + poll scheduler that publishes events.

A registered sensor callable takes the incoming payload and returns an `Event` (or
None to emit nothing); the host publishes it to the bus, where it triggers subscribed
workflows. v1 ships the webhook path; poll registration is recorded but the durable
scheduler lands with cron/watermarks (B7/B8). Executing user-authored sensor *modules*
(which import the `loopy` authoring shim) is a later refinement — see `loopy dev`,
which wires manifest webhooks to event publishers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi import FastAPI

from loopy_runtime.contract import Event

# A sensor callable: payload -> Event | None.
SensorFn = Callable[[dict], "Event | None"]


class FastAPISensorHost:
    def __init__(self, bus) -> None:
        self.bus = bus
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
        await self.bus.publish(event)
        return {"emitted": event.name}

    async def start(self, host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
        import uvicorn

        await uvicorn.Server(uvicorn.Config(self.app, host=host, port=port)).serve()
