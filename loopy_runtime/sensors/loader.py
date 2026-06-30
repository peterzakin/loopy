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
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from loopy_runtime.contract import Event, Tick
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


def _model_to_event(model: object) -> Event:
    """Wrap one `loopy.events` pydantic model instance in a runtime Event."""
    if not hasattr(model, "model_dump"):  # a generated loopy.events pydantic model
        raise TypeError(
            f"sensor returned {type(model).__name__}; expected a loopy.events model"
        )
    return Event(
        name=type(model).__name__,
        fields=model.model_dump(),
        id=f"sensor-{next(_ids)}",
        emitted_at=datetime.now(UTC),
    )


def to_events(result: object) -> list[Event]:
    """Normalize a sensor's return to a list of Events. Handles `None` (emit nothing), a
    single `loopy.events` model, or an `Iterable` of them (a poll fan-out / a `yield`-ing
    webhook). A model is itself iterable over its fields, so it must be checked first."""
    if result is None:
        return []
    if hasattr(result, "model_dump"):
        return [_model_to_event(result)]
    if isinstance(result, Iterable) and not isinstance(result, str | bytes):
        return [_model_to_event(item) for item in result]
    raise TypeError(
        f"sensor returned {type(result).__name__}; "
        "expected a loopy.events model, an iterable of them, or None"
    )


def normalize(result: object, expected_emits: str) -> list[Event]:
    """Convert a sensor return to Events and enforce the declared `emits` contract on each."""
    events = to_events(result)
    for event in events:
        if event.name != expected_emits:
            raise ValueError(
                f"sensor declared emits '{expected_emits}' but returned a '{event.name}' event"
            )
    return events


def load_webhook_sensor(spec: SensorSpec, root: str | Path) -> Callable[[dict], Event | None]:
    """Return a callable `payload -> Event | None` that runs the real sensor function and
    enforces that what it returns matches the declared `emits`. The webhook delivery path
    carries at most one event per request; the first normalized event is returned (None if
    the sensor emitted nothing)."""
    fn = _import_fn(spec.module, spec.fn, root)

    def invoke(payload: dict) -> Event | None:
        events = normalize(fn(Request(payload)), spec.emits)
        return events[0] if events else None

    return invoke


def builtin_webhook_sensor(spec: SensorSpec) -> Callable[[dict], Event | None]:
    """Resolve a platform-shipped built-in sensor: look its payload->fields mapper up by
    `emits` (no user module to import) and wrap the result in a runtime `Event`. The mapper
    returns None for deliveries that aren't this event's concern."""
    from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS

    mapper = BUILTIN_MAPPERS.get(spec.emits)
    if mapper is None:
        raise KeyError(f"no built-in mapper registered for '{spec.emits}'")

    def invoke(payload: dict) -> Event | None:
        fields = mapper(payload)
        if fields is None:
            return None
        return Event(
            name=spec.emits,
            fields=fields,
            id=f"builtin-{next(_ids)}",
            emitted_at=datetime.now(UTC),
        )

    return invoke


def load_poll_sensor(spec: SensorSpec, root: str | Path) -> Callable[[Tick], list[Event]]:
    """Mirror of `load_webhook_sensor` for poll sensors: return a callable `Tick ->
    list[Event]` that runs the real sensor function with a scheduler `Tick` (not a webhook
    `Request`) and normalizes its return — fan-out to many events — against the declared
    `emits`."""
    fn = _import_fn(spec.module, spec.fn, root)

    def invoke(tick: Tick) -> list[Event]:
        return normalize(fn(tick), spec.emits)

    return invoke
