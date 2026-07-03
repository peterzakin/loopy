"""Built-in Sentry webhook mappers — the runtime half of the Sentry built-in catalog.

A Sentry Custom (internal) Integration posts every subscribed resource to the single
webhook URL configured on it, discriminated by the `Sentry-Hook-Resource` header and the
body's `action`. The mappers here discriminate on the body alone — the `issue` resource
carries `data.issue`, the `event_alert` resource carries `data.event` — so the runner
needs no header plumbing. One delivery maps to one built-in event's fields, or to None
when the delivery isn't this event's concern (the same fan-out contract as
`github_builtins.py`).

The field set each mapper returns must match its contract in `loopy_core/builtins.py`
(`SENTRY_EVENTS`); `tests/test_builtins.py` asserts the two never drift.
"""

from __future__ import annotations

from collections.abc import Callable

# payload -> field dict, or None to emit nothing for this delivery.
Mapper = Callable[[dict], "dict | None"]

# The contract types `level` as this closed enum; Sentry payloads occasionally carry other
# strings (e.g. "sample"), which are clamped to "error" so the event still validates.
_LEVELS = frozenset({"debug", "info", "warning", "error", "fatal"})


def _issue(body: dict, action: str) -> dict | None:
    """The `data.issue` object iff this delivery is the issue webhook with `action`."""
    issue = (body.get("data") or {}).get("issue")
    if body.get("action") != action or not issue:
        return None
    return issue


def _fields(issue: dict) -> dict:
    """The fields shared by both issue events."""
    return {
        "issue_id": str(issue["id"]),  # Sentry sends a string; str() guards an int payload
        "title": issue["title"],
        "project": (issue.get("project") or {}).get("slug", ""),
        # `permalink` can be null on issue webhooks; fall back to "" rather than fabricate.
        "url": issue.get("permalink") or issue.get("web_url") or "",
    }


def _issue_created(body: dict) -> dict | None:
    issue = _issue(body, "created")
    if issue is None:
        return None
    level = issue.get("level")
    return _fields(issue) | {
        "culprit": issue.get("culprit") or "",
        "level": level if level in _LEVELS else "error",
    }


def _issue_resolved(body: dict) -> dict | None:
    issue = _issue(body, "resolved")
    if issue is None:
        return None
    return _fields(issue)


def _rule_name(data: dict) -> str:
    """The alert rule that fired. The integration platform sends `data.triggered_rule` (a
    string); the legacy plugin used `triggering_rules` (a list, sometimes on the event).
    Accept whichever is present."""
    single = data.get("triggered_rule")
    if isinstance(single, str) and single:
        return single
    for holder in (data, data.get("event") or {}):
        rules = holder.get("triggering_rules")
        if isinstance(rules, list) and rules:
            return str(rules[0])
    return ""


def _alert_triggered(body: dict) -> dict | None:
    """An issue-alert rule fired: the `event_alert` resource, `action: triggered`, carrying
    `data.event` (a full error event). Distinct from the issue resource, which has
    `data.issue` and no `data.event`."""
    data = body.get("data") or {}
    event = data.get("event")
    if body.get("action") != "triggered" or not event:
        return None
    level = event.get("level")
    return {
        # `issue_id` is the group id; older payloads spell it `groupID`.
        "issue_id": str(event.get("issue_id") or event.get("groupID") or ""),
        "title": event.get("title") or event.get("message") or "",
        "culprit": event.get("culprit") or "",
        "level": level if level in _LEVELS else "error",
        "rule": _rule_name(data),
        "url": event.get("web_url") or event.get("url") or "",
    }


MAPPERS: dict[str, Mapper] = {
    "Sentry.IssueCreated": _issue_created,
    "Sentry.IssueResolved": _issue_resolved,
    "Sentry.AlertTriggered": _alert_triggered,
}
