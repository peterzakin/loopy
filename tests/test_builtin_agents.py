"""Built-in agents (Option A): zero-config `BaseClaude` / `BaseCodex`.

A step may name a platform-shipped agent in its `agent:` without a `registry.yml` entry;
the compiler injects the referenced one (a fixed harness on the reserved `default` sandbox,
no skills) so `cross_check`'s X2 resolves it like any registered agent. Covers the happy
path per runtime, the manifest round-trip, user override, unreferenced non-injection, the
missing-sandbox error (E502), and that an unknown agent still falls through to E501.
"""

from __future__ import annotations

from loopy_core.builtins import BUILTIN_AGENTS
from loopy_core.compile import codes
from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

# A project with a `default` sandbox but *no* agents declared — a built-in `agent:` is the
# only way a step resolves. Triggering on a built-in event keeps it zero-registry.
REGISTRY = "sandboxes: { default: { provider: local } }\n"


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def test_base_claude_resolves_with_no_registry_entry(tmp_path):
    """`agent: BaseClaude` compiles clean with no agent in registry.yml."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseClaude"),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)

    agent = result.project.registry.agents["BaseClaude"]
    assert agent.harness.runtime == "claude-code"
    assert agent.harness.model == "claude-sonnet-4-6"
    assert agent.sandbox == "default"
    assert agent.skills == []  # a built-in ships no project-specific skills


def test_base_codex_resolves_with_no_registry_entry(tmp_path):
    """The Codex built-in binds the codex runtime."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseCodex"),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)
    agent = result.project.registry.agents["BaseCodex"]
    assert agent.harness.runtime == "codex"
    assert agent.harness.model == "gpt-5.5"


def test_builtin_agent_survives_to_manifest(tmp_path):
    """An injected built-in agent serializes like any other — the runtime needs its harness."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseClaude"),
        },
    )
    agents = to_manifest(compile_project(tmp_path).project)["registry"]["agents"]
    assert agents["BaseClaude"] == {
        "harness": {"runtime": "claude-code", "model": "claude-sonnet-4-6"},
        "sandbox": "default",
        "skills": [],
    }


def test_user_definition_overrides_builtin(tmp_path):
    """A declared agent of the same name wins — the built-in only fills a gap."""
    write_project(
        tmp_path,
        {
            "registry.yml": (
                "sandboxes: { default: { provider: local } }\n"
                "agents:\n"
                "  BaseClaude:\n"
                "    sandbox: default\n"
                "    harness: { runtime: claude-code, model: claude-opus-4-8 }\n"
            ),
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseClaude"),
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors(), result_codes(result)
    # The user's model, not the built-in's, is what compiled.
    assert result.project.registry.agents["BaseClaude"].harness.model == "claude-opus-4-8"


def test_unreferenced_builtin_agents_are_not_injected(tmp_path):
    """Only a built-in a step actually names is registered (no catalog dump)."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseClaude"),
        },
    )
    agents = compile_project(tmp_path).project.registry.agents
    assert "BaseClaude" in agents
    assert "BaseCodex" not in agents  # never referenced -> never injected


def test_builtin_agent_without_default_sandbox_reports_e502(tmp_path):
    """A built-in binds the reserved `default` sandbox; a project must still supply one."""
    write_project(
        tmp_path,
        {
            # A sandbox exists, but not one named `default`.
            "registry.yml": "sandboxes: { BaseSandbox: { provider: local } }\n",
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseClaude"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E502)


def test_unknown_agent_still_reports_e501(tmp_path):
    """A non-built-in, undeclared agent is unchanged — X2 still flags it."""
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/review/r.md": md("on: Github.PullRequestOpened\nagent: BaseGemini"),
        },
    )
    result = compile_project(tmp_path)
    assert_code(result, codes.E501)
    assert "BaseGemini" not in result.project.registry.agents


def test_catalog_covers_both_runtimes():
    """The two shipped built-ins name distinct runtimes — the single guard on the catalog shape."""
    runtimes = {spec["runtime"] for spec in BUILTIN_AGENTS.values()}
    assert runtimes == {"claude-code", "codex"}
