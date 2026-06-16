"""Authoring shim — the `sensor` decorator that user sensor modules import.

`loopy compile` writes a project-local `loopy/__init__.py` that re-exports this, so
user code does `from loopy import sensor`. The decorator only records config and marks
the function — it never runs at compile time (the frontend inspects sensors by AST);
it's consulted at run time by the backend's sensor loader.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Attribute the decorator stamps onto a sensor function.
SENSOR_ATTR = "__loopy_sensor__"


def sensor(
    *, webhook: str | None = None, poll: str | None = None, emits: str
) -> Callable[[Callable], Callable]:
    """Mark a function as a sensor. `emits` is the declared event; one of webhook/poll
    is the trigger. Returns the function unchanged (callable as normal)."""

    def decorate(fn: Callable) -> Callable:
        setattr(fn, SENSOR_ATTR, {"webhook": webhook, "poll": poll, "emits": emits})
        return fn

    return decorate


def sensor_config(fn: Callable) -> dict[str, Any] | None:
    """The recorded `@sensor` config for `fn`, or None if it isn't a sensor."""
    return getattr(fn, SENSOR_ATTR, None)
