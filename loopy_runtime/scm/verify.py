"""Confirm SCM outputs an agent claims (#19).

A step's `output.pr_url` is the agent's own parsed JSON — the runtime reports the run
green with no cross-check that the PR object actually exists on GitHub, so a fabricated
or malformed URL still reads as success. These helpers parse a claimed PR URL and confirm
it against the API with the already-minted installation token, producing an `SCMVerification`
the runtime annotates onto the run and the CLI surfaces.

Pure except for the one `GET` (via `github_app.get_pull_request`), so the parsing and
status mapping are unit-testable without the wire. A check that can't *run* (network/API
hiccup) is reported as `unverifiable` — distinct from a `not_found`/`malformed` that the
agent is attributable for — so a transient failure never masquerades as a fabricated PR.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from loopy_runtime.scm import github_app

# Output fields whose value is treated as a GitHub PR URL to confirm. `pr_url` is the field
# the scaffold + examples document (`pr_url: url`); kept as a tuple so a sibling SCM field can
# join without touching the call sites.
PR_URL_FIELDS: tuple[str, ...] = ("pr_url",)

# A GitHub PR web URL: https://github.com/{owner}/{repo}/pull/{number} (with or without scheme,
# trailing path/query tolerated). `search` so a URL wrapped in surrounding text still parses.
_PR_URL_RE = re.compile(r"github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)")


@dataclass(frozen=True)
class SCMVerification:
    """The outcome of confirming one claimed SCM URL against the provider."""

    field: str
    url: str
    status: str  # confirmed | not_found | malformed | unverifiable
    detail: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def fabricated(self) -> bool:
        """A deterministic, agent-attributable failure: the URL is malformed or names a PR
        that GitHub says doesn't exist. Distinct from a transient `unverifiable` so callers
        can fail a run on a fabricated URL without failing it on a flaky API call."""
        return self.status in ("malformed", "not_found")

    def as_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "url": self.url, "status": self.status, "detail": self.detail}


def pr_url_targets(fields: Mapping[str, object]) -> list[tuple[str, str]]:
    """The `(field, url)` pairs in a step output that name a GitHub PR to confirm.

    Only non-empty string values of the recognized fields qualify — a missing or non-string
    `pr_url` simply yields nothing to verify (the step had no SCM claim to cross-check)."""
    targets: list[tuple[str, str]] = []
    for field in PR_URL_FIELDS:
        value = fields.get(field)
        if isinstance(value, str) and value.strip():
            targets.append((field, value.strip()))
    return targets


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """`(owner, repo, number)` from a GitHub PR web URL, or None if it isn't one."""
    match = _PR_URL_RE.search(url)
    if match is None:
        return None
    return match["owner"], match["repo"], int(match["number"])


def verify_pr_url(
    field: str, url: str, token: str, *, base_url: str = github_app.GITHUB_API
) -> SCMVerification:
    """Confirm a claimed PR URL exists on GitHub with `token`, returning an `SCMVerification`.

    Never raises: a malformed URL is `malformed`, a 404 is `not_found` (the PR was never
    created), any other API/transport error is `unverifiable` (the check couldn't run)."""
    parsed = parse_pr_url(url)
    if parsed is None:
        return SCMVerification(field, url, "malformed", "not a github.com pull-request URL")
    owner, repo, number = parsed
    try:
        github_app.get_pull_request(token, owner, repo, number, base_url=base_url)
    except github_app.GitHubAPIError as exc:
        if exc.status == 404:
            return SCMVerification(
                field, url, "not_found", f"GitHub has no PR #{number} in {owner}/{repo}"
            )
        return SCMVerification(field, url, "unverifiable", str(exc))
    except Exception as exc:  # noqa: BLE001 - any transport failure is 'couldn't verify', not 'fake'
        return SCMVerification(field, url, "unverifiable", str(exc))
    return SCMVerification(field, url, "confirmed", None)
