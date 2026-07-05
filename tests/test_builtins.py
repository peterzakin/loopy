"""Built-in events (Option A): zero-config `Github.*` triggers.

A workflow may trigger on a platform-shipped event without declaring it or authoring a
sensor; the compiler injects the contract + a built-in sensor. Covers the happy path,
codegen exclusion, the reserved-namespace guard (E215), and unknown built-ins (E112),
plus the contract/mapper agreement that keeps the core and runtime halves in lockstep.
"""

from __future__ import annotations

from loopy_core.builtins import BUILTIN_PROVIDERS, GITHUB_EVENTS
from loopy_core.compile import codes
from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from loopy_core.events.codegen import generate_events
from loopy_runtime.scm.builtin_registry import MAPPERS_BY_PROVIDER
from loopy_runtime.scm.github_builtins import BUILTIN_MAPPERS
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

# A minimal project that compiles clean — one local sandbox + agent, no events declared.
REGISTRY = (
    "defaults: { agent: { sandbox: default, model: claude-sonnet-4-6, harness: claude-code } }\n"
    "sandboxes: { default: { provider: local } }\n"
    "agents: { Reviewer: {} }\n"
)


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def test_workflow_triggers_on_builtin_with_no_declarations(tmp_path):
    """`on: Github.PullRequestOpened` compiles clean with no event entry and no sensor file."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md(
                "on: Github.PullRequestOpened\nagent: Reviewer",
                body="Review PR #{{ event.number }} on {{ event.repo }} ({{ event.base }}).",
            ),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)

    # The event contract was injected as a real registered (built-in) event.
    event = result.project.registry.events["Github.PullRequestOpened"]
    assert event.builtin is True
    assert event.fields["number"] == {"type": "integer"}

    # A built-in sensor was synthesized on /hooks/github with no user module.
    sensors = [s for s in result.project.sensors if s.emits == "Github.PullRequestOpened"]
    assert len(sensors) == 1
    s = sensors[0]
    assert s.source == "builtin"
    assert s.provider == "github"
    assert s.module is None and s.fn is None
    assert s.trigger.kind == "webhook" and s.trigger.path == "/hooks/github"

    # And it survives to the manifest with the discriminator intact.
    specs = to_manifest(result.project)["sensors"]
    spec = next(x for x in specs if x["emits"].startswith("Github."))
    assert spec["source"] == "builtin"
    assert spec["module"] is None


def test_unreferenced_builtins_are_not_injected(tmp_path):
    """Only built-ins a workflow actually triggers on are registered (no catalog dump)."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Reviewer"),
        },
    )
    events = compile_project(tmp_path).project.registry.events
    assert "Github.PullRequestOpened" in events
    assert "Github.Push" not in events  # never referenced -> never injected


def test_builtin_field_ref_is_validated(tmp_path):
    """A bad field on a built-in event is caught by the same T1 check as any event (E302)."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md(
                "on: Github.PullRequestOpened\nagent: Reviewer",
                body="Nonsense: {{ event.nope }}",
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E302)


def test_unknown_builtin_reports_e112(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.Nonexistent\nagent: Reviewer"),
        },
    )
    result = compile_project(tmp_path)
    assert_code(result, codes.E112)
    # The reserved namespace defers E504 to the built-in pass, so a typo'd built-in is E112 alone.
    assert codes.E504 not in result_codes(result)


def test_user_event_in_reserved_namespace_reports_e215(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY + "events:\n  Github.Custom: { x: str }\n",
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Reviewer"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E215)


def test_user_sensor_in_reserved_namespace_reports_e215(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Reviewer"),
            "sensors/s.py": (
                "from loopy import sensor\n\n\n"
                '@sensor(webhook="/hooks/github", emits="Github.PullRequestOpened")\n'
                "def hijack(req):\n    return None\n"
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E215)


def test_builtin_events_excluded_from_codegen(tmp_path):
    """Built-in events must not appear in generated `loopy.events` — a dotted name isn't a
    valid Python class, and their mappers ship in the runtime, not user code."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY + "events:\n  Local: { x: str }\n",
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: Reviewer"),
        },
    )
    registry = compile_project(tmp_path).project.registry
    module = generate_events(registry)["loopy/events.py"]
    assert "class Local(BaseModel):" in module
    assert "Github." not in module  # built-in excluded


def test_catalog_and_mappers_agree():
    """The core contracts and the runtime mappers must cover the same events with the same
    field names — the single guard against the two halves drifting. Every provider in the
    catalog must have a runtime mapper set (and vice versa), and each pair must cover the same
    events. Driven off the registry so a new provider is covered without editing this test."""
    assert {p.name for p in BUILTIN_PROVIDERS} == set(MAPPERS_BY_PROVIDER)
    for provider in BUILTIN_PROVIDERS:
        assert set(provider.events) == set(MAPPERS_BY_PROVIDER[provider.name]), provider.name


def test_each_mapper_returns_exactly_its_contract_fields():
    """A representative valid payload through each mapper yields the contract's field set."""
    payloads = {
        "Github.PullRequestOpened": {
            "action": "opened",
            "repository": {"full_name": "octo/hello"},
            "pull_request": {
                "number": 1,
                "title": "Add widget",
                "head": {"ref": "feat"},
                "base": {"ref": "main"},
                "html_url": "https://gh/pr/1",
            },
        },
        "Github.PullRequestMerged": {
            "action": "closed",
            "repository": {"full_name": "octo/hello"},
            "pull_request": {
                "number": 1,
                "title": "Add widget",
                "merged": True,
                "merged_by": {"login": "octocat"},
                "html_url": "https://gh/pr/1",
            },
        },
        "Github.IssueOpened": {
            "action": "opened",
            "repository": {"full_name": "octo/hello"},
            "issue": {
                "number": 7,
                "title": "Bug",
                "body": "broken",
                "user": {"login": "octocat"},
                "html_url": "https://gh/i/7",
            },
        },
        "Github.IssueCommentCreated": {
            "action": "created",
            "repository": {"full_name": "octo/hello"},
            "issue": {"number": 7, "pull_request": {}},
            "comment": {
                "body": "thoughts",
                "user": {"login": "octocat"},
                "html_url": "https://gh/c/1",
            },
        },
        "Github.Push": {
            "repository": {"full_name": "octo/hello"},
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "after": "b" * 40,
            "pusher": {"name": "octocat"},
            "commits": [{}, {}],
        },
    }
    for name, mapper in BUILTIN_MAPPERS.items():
        fields = mapper(payloads[name])
        assert fields is not None, name
        assert set(fields) == set(GITHUB_EVENTS[name]), name


def test_mappers_ignore_unrelated_deliveries():
    """Each mapper returns None for a delivery that isn't its concern (the fan-out contract)."""
    pr_closed_unmerged = {
        "action": "closed",
        "repository": {"full_name": "octo/hello"},
        "pull_request": {"merged": False},
    }
    assert BUILTIN_MAPPERS["Github.PullRequestMerged"](pr_closed_unmerged) is None
    assert BUILTIN_MAPPERS["Github.PullRequestOpened"](pr_closed_unmerged) is None
    # A push delivery isn't an issue/PR/comment.
    push = {"ref": "refs/heads/main", "pusher": {"name": "x"}, "repository": {"full_name": "o/h"}}
    assert BUILTIN_MAPPERS["Github.IssueOpened"](push) is None
    assert BUILTIN_MAPPERS["Github.Push"](push) is not None
