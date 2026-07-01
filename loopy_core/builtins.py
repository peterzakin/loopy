"""Built-in event + agent catalog (Option A) — platform-shipped entities a workflow may
use with zero declarations.

A workflow that writes `on: Github.PullRequestOpened` gets the event contract *and*
its producing sensor injected by the compiler (`compile/builtins.py`); the developer
writes no `registry.yml` entry and no `sensors/` module. This module owns the
*contracts* (field name -> terse type, desugared like any registry event). The
matching payload->fields *mappers* live in `loopy_runtime/scm/github_builtins.py`;
`tests/test_builtins.py` asserts the two halves never drift.

Likewise `BaseClaude` and `BaseCodex` ship as built-in **agents**: a step may name either
in its `agent:` with no `registry.yml` entry. The compiler injects the referenced one (a
fixed harness on the reserved `default` sandbox, no skills) so a project can drive a Claude
Code or Codex runtime out of the box. A user who declares an agent of the same name overrides
the built-in — their definition wins.

The `Github.` prefix is reserved: a user may not declare an event or author a sensor
in it (enforced in `compile/builtins.py`). Names are Capitalized so they read like
the existing event namespace; the dotted form mirrors GitHub's own event/action.
"""

from __future__ import annotations

# Reserved namespace — user events/sensors may not use this prefix.
GITHUB_PREFIX = "Github."

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


def is_reserved(name: str) -> bool:
    """True if `name` lives in a reserved built-in namespace."""
    return name.startswith(GITHUB_PREFIX)
