"""Event validation against the registry contract — the ingress gate (Stage 1).

The `SensorRunner` is untrusted (developer code, possibly another language, possibly in
the developer's own app), so the `EventReceiver` re-validates every event against the
manifest registry before it reaches the bus. That is what lets the `Runtime` trust
whatever it consumes. Validation is **structural** — event name, required fields, and
each field's declared type/enum — not semantic (it checks `issue_id` is a present
string, not that it names a real issue).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import EventContract


class EventValidationError(ValueError):
    """An event failed validation against the registry contract — rejected at ingress."""


# JSON-Schema `type` → predicate. bool is excluded from int/number (it subclasses int).
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, Mapping),
}


def validate_event(event: Event, events: Mapping[str, EventContract]) -> None:
    """Raise `EventValidationError` unless `event` matches its registered contract.

    Extra fields beyond the contract are allowed (additive evolution); missing required
    fields, an unregistered name, or a type/enum mismatch are rejected.
    """
    contract = events.get(event.name)
    if contract is None:
        known = ", ".join(sorted(events)) or "(none registered)"
        raise EventValidationError(
            f"unknown event {event.name!r}: not in the registry. Registered: {known}"
        )

    missing = sorted(f for f in contract.fields if f not in event.fields)
    if missing:
        raise EventValidationError(
            f"event {event.name!r} is missing required field(s): {missing}"
        )

    for field_name, schema in contract.fields.items():
        value = event.fields[field_name]
        kind = schema.get("type")
        check = _TYPE_CHECKS.get(kind)
        if check is not None and not check(value):
            raise EventValidationError(
                f"event {event.name!r} field {field_name!r}: expected {kind}, "
                f"got {type(value).__name__}"
            )
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise EventValidationError(
                f"event {event.name!r} field {field_name!r}: {value!r} is not one of {enum}"
            )
