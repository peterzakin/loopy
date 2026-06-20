"""SensorRunner (B1 ingress) — the webhook server that injects events.

Hosts each `@sensor(webhook=…)` as an HTTP route: a registered sensor callable takes the
incoming payload and returns an `Event` (or None to emit nothing), which is handed to the
`EventReceiver` (validate → publish). This is the *push* edge only; *poll* (timer) sensors
are driven by the separate `PollScheduler` (`sensors/scheduler.py`) — both feed the same
receiver but through different trigger mechanisms. Executing user-authored sensor *modules*
(which import the `loopy` authoring shim) is handled by `loopy run`'s sensor loader;
`synthesizing_publisher` is the dev fallback when a module can't be loaded.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request

from loopy_runtime.contract import Event, EventReceiver
from loopy_runtime.payloads import synthesize_fields
from loopy_runtime.scm.github_webhook import SignatureError
from loopy_runtime.validation import EventValidationError

if TYPE_CHECKING:
    from loopy_runtime.manifest_model import Manifest, SensorSpec

# A sensor callable: payload -> Event | None.
SensorFn = Callable[[dict], "Event | None"]
# A signature verifier: raw body + request headers -> None, raising on a bad signature.
Verifier = Callable[[bytes, Mapping[str, str]], None]


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
        # Many sensors may share one path: a provider like GitHub posts every event type
        # (PR opened, PR merged, push, …) to a single URL, so we fan one delivery out to
        # all sensors registered on that path and emit whichever events match.
        self._sensors: dict[str, list[SensorFn]] = {}
        self._verifiers: dict[str, Verifier | None] = {}

    def register_webhook(self, path: str, fn: SensorFn, *, verify: Verifier | None = None) -> None:
        """Register `fn` on `path`; calling again with the same path adds another sensor to
        the fan-out. `verify` (set on the first registration for a path) gates the path with
        a signature check on the raw body — used for GitHub's `X-Hub-Signature-256`."""
        first = path not in self._sensors
        self._sensors.setdefault(path, []).append(fn)
        if first:
            self._verifiers[path] = verify
            self.webhook_paths.append(path)
            self._install_route(path)

    def _install_route(self, path: str) -> None:
        verifier = self._verifiers.get(path)
        if verifier is None:
            # Unsigned path (the original contract): FastAPI parses the JSON body for us.
            async def handler(payload: dict | None = None):
                return await self._dispatch_path(path, payload or {})
        else:
            # Signed path: read the *raw* bytes (re-serialized JSON wouldn't match the HMAC),
            # verify, then parse and fan out.
            async def handler(request: Request):  # type: ignore[misc]
                raw = await request.body()
                try:
                    verifier(raw, request.headers)
                except SignatureError as exc:
                    raise HTTPException(status_code=401, detail=str(exc)) from exc
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    raise HTTPException(status_code=400, detail="invalid JSON body") from exc
                return await self._dispatch_path(path, payload)

        self.app.add_api_route(path, handler, methods=["POST"])

    async def _dispatch_path(self, path: str, payload: dict) -> dict:
        """Fan one delivery out to every sensor on `path`; return the names actually emitted
        (sensors that don't match this delivery return None and contribute nothing)."""
        emitted: list[str] = []
        for fn in self._sensors.get(path, ()):
            try:
                result = await self._dispatch(fn, payload)
            except EventValidationError as exc:
                # The produced event failed the registry contract: a client error, not a
                # 500. 422 Unprocessable Entity with the validation detail.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if result["emitted"] is not None:
                emitted.append(result["emitted"])
        return {"emitted": emitted}

    async def _dispatch(self, fn: SensorFn, payload: dict) -> dict:
        event = fn(payload)
        if event is None:
            return {"emitted": None}
        await self.receiver.receive(event)
        return {"emitted": event.name}

    async def start(self, host: str = "127.0.0.1", port: int = 8000) -> None:  # pragma: no cover
        import uvicorn

        await uvicorn.Server(uvicorn.Config(self.app, host=host, port=port)).serve()
