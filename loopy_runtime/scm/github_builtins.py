"""Built-in GitHub webhook mappers — the runtime half of the built-in catalog.

One signed GitHub delivery (`payload`) maps to one built-in event's fields, or to None
when the delivery isn't this event's concern. GitHub posts every event type to the single
`/hooks/github` URL, so the runner fans each delivery out to every built-in sensor and only
the matching mapper returns fields; the rest return None. Mappers discriminate by payload
shape (`action`, which top-level object is present, merged-state) — the same technique the
hand-written `examples/github` sensors use.

The field set each mapper returns must match its contract in `loopy_core/builtins.py`
(`GITHUB_EVENTS`); `tests/test_builtins.py` asserts the two never drift.
"""

from __future__ import annotations

from collections.abc import Callable

# payload -> field dict, or None to emit nothing for this delivery.
Mapper = Callable[[dict], "dict | None"]


def _pull_request_opened(body: dict) -> dict | None:
    pr = body.get("pull_request")
    if body.get("action") != "opened" or not pr:
        return None
    return {
        "number": pr["number"],
        "repo": body["repository"]["full_name"],
        "title": pr["title"],
        "branch": pr["head"]["ref"],
        "base": pr["base"]["ref"],
        "url": pr["html_url"],
    }


def _pull_request_merged(body: dict) -> dict | None:
    pr = body.get("pull_request") or {}
    # A merge arrives as action=closed with merged=true; a plain close has merged=false.
    if body.get("action") != "closed" or not pr.get("merged"):
        return None
    return {
        "number": pr["number"],
        "repo": body["repository"]["full_name"],
        "title": pr["title"],
        "url": pr["html_url"],
        "merged_by": (pr.get("merged_by") or {}).get("login", "unknown"),
    }


def _issue_opened(body: dict) -> dict | None:
    issue = body.get("issue")
    # The `issues` event carries `issue`; `pull_request` deliveries don't (a PR is a
    # separate event type), so the presence of `issue` disambiguates from PullRequestOpened.
    if body.get("action") != "opened" or not issue:
        return None
    return {
        "number": issue["number"],
        "repo": body["repository"]["full_name"],
        "title": issue["title"],
        "body": issue.get("body") or "",
        "author": (issue.get("user") or {}).get("login", "unknown"),
        "url": issue["html_url"],
    }


def _issue_comment_created(body: dict) -> dict | None:
    comment = body.get("comment")
    issue = body.get("issue") or {}
    if body.get("action") != "created" or not comment:
        return None
    return {
        "repo": body["repository"]["full_name"],
        "issue_number": issue.get("number", 0),
        "body": comment.get("body") or "",
        "author": (comment.get("user") or {}).get("login", "unknown"),
        "url": comment.get("html_url", ""),
        # GitHub files PR comments under issue_comment too; `issue.pull_request` marks those.
        "on_pull_request": "pull_request" in issue,
    }


def _push(body: dict) -> dict | None:
    # The push event has no `action`; it carries ref/before/after/pusher and no issue/PR object.
    if "ref" not in body or "pusher" not in body or "action" in body:
        return None
    return {
        "repo": body["repository"]["full_name"],
        "ref": body["ref"],
        "before": body.get("before", ""),
        "after": body.get("after", ""),
        "pusher": (body.get("pusher") or {}).get("name", "unknown"),
        "commit_count": len(body.get("commits") or []),
    }


BUILTIN_MAPPERS: dict[str, Mapper] = {
    "Github.PullRequestOpened": _pull_request_opened,
    "Github.PullRequestMerged": _pull_request_merged,
    "Github.IssueOpened": _issue_opened,
    "Github.IssueCommentCreated": _issue_comment_created,
    "Github.Push": _push,
}
