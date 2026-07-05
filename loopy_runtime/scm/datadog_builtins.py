"""Built-in Datadog webhook mappers — the runtime half of the Datadog built-in catalog.

Datadog differs from GitHub and Sentry in a way that shapes this whole module: its Webhooks
integration sends **no fixed payload**. The body is whatever template the user configures in
the integration tile from `$`-variables, so there is no canonical Datadog wire shape to map.
The built-in therefore prescribes a canonical payload (documented in
`docs/plans/integrations/datadog.md` and on the landing integrations page) that the user
pastes once; these mappers read exactly that shape:

    {"event": "monitor", "id": "$ALERT_ID", "transition": "$ALERT_TRANSITION",
     "alert_type": "$ALERT_TYPE", "priority": "$ALERT_PRIORITY", "title": "$EVENT_TITLE",
     "body": "$EVENT_MSG", "metric": "$ALERT_METRIC", "scope": "$ALERT_SCOPE",
     "host": "$HOSTNAME", "tags": "$TAGS", "url": "$LINK", "date": "$DATE"}

Both events discriminate on the body's `transition` field alone (no `Sentry-Hook-Resource`
style header), so the runner needs no header plumbing — the same body-only fan-out contract as
`github_builtins.py` / `sentry_builtins.py`: one delivery maps to one event's fields, or to
None when the delivery isn't this event's concern.

The field set each mapper returns must match its contract in `loopy_core/builtins.py`
(`DATADOG_EVENTS`); `tests/test_builtins.py` asserts the two never drift.
"""

from __future__ import annotations

from collections.abc import Callable

# payload -> field dict, or None to emit nothing for this delivery.
Mapper = Callable[[dict], "dict | None"]

# Transitions that mean "a monitor is in alert" — folded into one event; the severity nuance
# lives in `alert_type`/`priority`/`transition`, which the workflow can branch on in prose.
_ALERT_TRANSITIONS = frozenset({"Triggered", "Re-Triggered", "Warn"})
# The contract types `alert_type` as this closed enum; recovery deliveries and some monitor
# types emit other strings, which are clamped to "error" so the event still validates.
_ALERT_TYPES = frozenset({"error", "warning", "info", "success"})


def _is_monitor(body: dict) -> bool:
    """True when this delivery came from the canonical monitor template. The `"event":
    "monitor"` literal is the primary discriminator; `transition` present is a fallback for a
    template that omitted the literal."""
    return body.get("event") == "monitor" or "transition" in body


def _monitor_alerted(body: dict) -> dict | None:
    if not _is_monitor(body) or body.get("transition") not in _ALERT_TRANSITIONS:
        return None
    alert_type = body.get("alert_type")
    return {
        "monitor_id": str(body.get("id") or ""),
        "title": body.get("title") or "",
        "body": body.get("body") or "",
        "alert_type": alert_type if alert_type in _ALERT_TYPES else "error",
        "priority": body.get("priority") or "",
        "metric": body.get("metric") or "",
        "scope": body.get("scope") or "",
        "host": body.get("host") or "",
        "transition": body.get("transition") or "",
        "url": body.get("url") or "",
    }


def _monitor_recovered(body: dict) -> dict | None:
    if not _is_monitor(body) or body.get("transition") != "Recovered":
        return None
    return {
        "monitor_id": str(body.get("id") or ""),
        "title": body.get("title") or "",
        "scope": body.get("scope") or "",
        "host": body.get("host") or "",
        "url": body.get("url") or "",
    }


MAPPERS: dict[str, Mapper] = {
    "Datadog.MonitorAlerted": _monitor_alerted,
    "Datadog.MonitorRecovered": _monitor_recovered,
}
