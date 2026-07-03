"""Built-in Sentry webhook mappers — the runtime half of the Sentry built-in catalog.

A Sentry Custom (internal) Integration posts every subscribed resource to the single
webhook URL configured on it, discriminated by the `Sentry-Hook-Resource` header and the
body's `action`. The mappers here discriminate on the body alone — only the `issue`
resource carries `data.issue` — so the runner needs no header plumbing. One delivery maps
to one built-in event's fields, or to None when the delivery isn't this event's concern
(the same fan-out contract as `github_builtins.py`).

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


MAPPERS: dict[str, Mapper] = {
    "Sentry.IssueCreated": _issue_created,
    "Sentry.IssueResolved": _issue_resolved,
}
