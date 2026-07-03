"""Built-in event + agent catalog (Option A) — platform-shipped entities a workflow may
use with zero declarations.

A workflow that writes `on: Github.PullRequestOpened` (or `on: Sentry.IssueCreated`) gets
the event contract *and* its producing sensor injected by the compiler
(`compile/builtins.py`); the developer writes no `registry.yml` entry and no `sensors/`
module. This module owns the *contracts* (field name -> terse type, desugared like any
registry event), grouped per provider in `BUILTIN_PROVIDERS`. The matching
payload->fields *mappers* live in `loopy_runtime/scm/<provider>_builtins.py`;
`tests/test_builtins.py` asserts the two halves never drift.

Likewise `BaseClaude` and `BaseCodex` ship as built-in **agents**: a step may name either
in its `agent:` with no `registry.yml` entry. The compiler injects the referenced one (a
fixed harness on the reserved `default` sandbox, no skills) so a project can drive a Claude
Code or Codex runtime out of the box. A user who declares an agent of the same name overrides
the built-in — their definition wins.

Each provider's prefix (`Github.`, `Sentry.`) is reserved: a user may not declare an
event or author a sensor in it (enforced in `compile/builtins.py`). Names are Capitalized
so they read like the existing event namespace; the dotted form mirrors the provider's
own event/action vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reserved namespaces — user events/sensors may not use these prefixes.
GITHUB_PREFIX = "Github."
SENTRY_PREFIX = "Sentry."

# Built-in agents ship one per runtime; a step names either by `agent:` with no registry entry.
# The compiler binds them to the reserved `default` sandbox (a project supplies its own `default`
# sandbox — where compute runs is never inferred). agent name -> harness {runtime, model}.
BUILTIN_AGENT_SANDBOX = "default"
BUILTIN_AGENTS: dict[str, dict[str, str]] = {
    "BaseClaude": {"runtime": "claude-code", "model": "claude-sonnet-4-6"},
    "BaseCodex": {"runtime": "codex", "model": "gpt-5.5"},
}


def is_builtin_agent(name: str) -> bool:
    """True if `name` is a platform-shipped agent (injectable without a registry entry)."""
    return name in BUILTIN_AGENTS


# event name -> {field: terse type}. Kept in lockstep with BUILTIN_MAPPERS (runtime).
GITHUB_EVENTS: dict[str, dict[str, str]] = {
    "Github.PullRequestOpened": {
        "number": "int",
        "repo": "str",
        "title": "str",
        "branch": "str",  # head ref (the PR's source branch)
        "base": "str",  # base ref (what it merges into)
        "url": "url",
    },
    "Github.PullRequestMerged": {
        "number": "int",
        "repo": "str",
        "title": "str",
        "url": "url",
        "merged_by": "str",  # login of whoever merged it
    },
    "Github.IssueOpened": {
        "number": "int",
        "repo": "str",
        "title": "str",
        "body": "str",
        "author": "str",  # login of the issue's opener
        "url": "url",
    },
    "Github.IssueCommentCreated": {
        "repo": "str",
        "issue_number": "int",
        "body": "str",
        "author": "str",  # login of the commenter
        "url": "url",
        "on_pull_request": "bool",  # GitHub files PR comments under issue_comment too
    },
    "Github.Push": {
        "repo": "str",
        "ref": "str",  # e.g. refs/heads/main
        "before": "str",  # sha before the push
        "after": "str",  # sha after the push
        "pusher": "str",  # name of whoever pushed
        "commit_count": "int",
    },
}


# event name -> {field: terse type}. Kept in lockstep with the Sentry MAPPERS (runtime).
# Both events map Sentry's `issue` resource webhook, discriminated by the body's `action`.
SENTRY_EVENTS: dict[str, dict[str, str]] = {
    "Sentry.IssueCreated": {
        "issue_id": "str",
        "title": "str",
        "culprit": "str",  # where the error happened, e.g. "app/views.py in get"
        "level": "enum[debug, info, warning, error, fatal]",
        "project": "str",  # project slug
        "url": "url",  # permalink to the issue ("" when Sentry omits it)
    },
    "Sentry.IssueResolved": {
        "issue_id": "str",
        "title": "str",
        "project": "str",  # project slug
        "url": "url",  # permalink to the issue ("" when Sentry omits it)
    },
}


@dataclass(frozen=True)
class BuiltinProvider:
    """A platform-shipped webhook provider: its reserved event-name prefix, the single
    path all its deliveries arrive on, and its event catalog (name -> {field: terse
    type}). The runtime half — payload mappers and the signature verifier — is keyed by
    `name` (`Sensor.provider`) in `loopy_runtime/scm/`."""

    name: str  # matches Sensor.provider, e.g. "github"
    prefix: str  # reserved event-name prefix, e.g. "Github."
    webhook_path: str  # the one URL the provider posts every event type to
    events: dict[str, dict[str, str]]


BUILTIN_PROVIDERS: tuple[BuiltinProvider, ...] = (
    BuiltinProvider("github", GITHUB_PREFIX, "/hooks/github", GITHUB_EVENTS),
    BuiltinProvider("sentry", SENTRY_PREFIX, "/hooks/sentry", SENTRY_EVENTS),
)


def provider_for(name: str) -> BuiltinProvider | None:
    """The built-in provider whose reserved prefix `name` lives under, else None."""
    return next((p for p in BUILTIN_PROVIDERS if name.startswith(p.prefix)), None)


def is_reserved(name: str) -> bool:
    """True if `name` lives in a reserved built-in namespace."""
    return provider_for(name) is not None
