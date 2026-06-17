"""Event ⇄ JSON codec — the wire format for networked EventBuses (B5).

An `Event` is a JSON-serializable value type by design (ARCHITECTURE §3.4), so a broker
just needs a stable encoding. `fields` are already registry-validated to JSON-safe types
(str/int/bool/enum/url); `emitted_at` is carried as ISO-8601. The decode side is untrusted
input — the `EventReceiver`/registry re-validates the decoded event, this only reconstructs
the value type.
"""

from __future__ import annotations

import json
from datetime import datetime

from loopy_runtime.contract import Event


def encode_event(event: Event) -> str:
    """Serialize an Event to a JSON string for the wire."""
    return json.dumps(
        {
            "name": event.name,
            "fields": dict(event.fields),
            "id": event.id,
            "emitted_at": event.emitted_at.isoformat(),
        },
        sort_keys=True,
    )


def decode_event(raw: str) -> Event:
    """Reconstruct an Event from its JSON wire form. Re-validate downstream at the gate."""
    d = json.loads(raw)
    return Event(
        name=d["name"],
        fields=d["fields"],
        id=d["id"],
        emitted_at=datetime.fromisoformat(d["emitted_at"]),
    )
