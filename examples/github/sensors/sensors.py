"""GitHub webhook sensors — translate one signed GitHub delivery into a loopy event.

GitHub sends every subscribed event type to the single configured URL, so both sensors
below listen on `/hooks/github`. The runner verifies the `X-Hub-Signature-256` HMAC once
at the edge (when GITHUB_WEBHOOK_SECRET is set) and fans the delivery out to both; each
sensor inspects `action` (and, for merges, `pull_request.merged`) and returns None for
deliveries that aren't its concern. `req.json` is the parsed webhook payload.
"""

from loopy import sensor
from loopy.events import PullRequestMerged, PullRequestOpened  # generated from registry.yml


@sensor(webhook="/hooks/github", emits="PullRequestOpened")
def on_pull_request_opened(req) -> PullRequestOpened | None:
    body = req.json
    pr = body.get("pull_request")
    if body.get("action") != "opened" or pr is None:
        return None  # not a PR-opened delivery — let the other sensor (or nobody) handle it
    return PullRequestOpened(
        number=pr["number"],
        repo=body["repository"]["full_name"],
        title=pr["title"],
        branch=pr["head"]["ref"],
        base=pr["base"]["ref"],
        url=pr["html_url"],
    )


@sensor(webhook="/hooks/github", emits="PullRequestMerged")
def on_pull_request_merged(req) -> PullRequestMerged | None:
    body = req.json
    pr = body.get("pull_request") or {}
    # A merge arrives as action=closed with merged=true; a plain close has merged=false.
    if body.get("action") != "closed" or not pr.get("merged"):
        return None
    return PullRequestMerged(
        number=pr["number"],
        repo=body["repository"]["full_name"],
        title=pr["title"],
        url=pr["html_url"],
        merged_by=(pr.get("merged_by") or {}).get("login", "unknown"),
    )
