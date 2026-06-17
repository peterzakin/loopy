"""Load real `@sensor` functions from a project and adapt them to the SensorRunner.

Imports the user's sensor module (which does `from loopy import sensor` / `from
loopy.events import …` — both provided by what `loopy compile` generated), and wraps
the function so calling it with a request payload yields a runtime `Event`. This runs
user code at *run* time (the backend), never at compile time.
"""

from __future__ import annotations

import importlib
import itertools
import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import SensorSpec

_ids = itertools.count(1)


class Request:
    """Minimal request object passed to a sensor: `req.json` / `req.body` are the payload."""

    def __init__(self, payload: dict):
        self.json = payload
        self.body = payload


def _import_fn(module: str, fn_name: str, root: str | Path) -> Callable:
    root = str(Path(root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(module)
    return getattr(mod, fn_name)


def to_event(result: object) -> Event | None:
    """Normalize a sensor's return (a `loopy.events` model instance, or None) to an Event."""
    if result is None:
        return None
    if isinstance(result, list | tuple | types.GeneratorType):
        raise NotImplementedError(
            "multi-event sensors (yield / Iterator) are not supported in v1; return one event"
        )
    if hasattr(result, "model_dump"):  # a generated loopy.events pydantic model
        return Event(
            name=type(result).__name__,
            fields=result.model_dump(),
            id=f"sensor-{next(_ids)}",
            emitted_at=datetime.now(UTC),
        )
    raise TypeError(
        f"sensor returned {type(result).__name__}; expected a loopy.events model or None"
    )


def normalize(result: object, expected_emits: str) -> Event | None:
    """Convert a sensor return to an Event and enforce the declared `emits` contract."""
    event = to_event(result)
    if event is not None and event.name != expected_emits:
        raise ValueError(
            f"sensor declared emits '{expected_emits}' but returned a '{event.name}' event"
        )
    return event


def load_webhook_sensor(spec: SensorSpec, root: str | Path) -> Callable[[dict], Event | None]:
    """Return a callable `payload -> Event | None` that runs the real sensor function and
    enforces that what it returns matches the declared `emits`."""
    fn = _import_fn(spec.module, spec.fn, root)

    def invoke(payload: dict) -> Event | None:
        return normalize(fn(Request(payload)), spec.emits)

    return invoke
