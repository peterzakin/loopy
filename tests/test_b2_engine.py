"""B2 unit tests: renderer, budget, retry, secrets, and runtime guards."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopy_runtime.budget import BudgetEnforcer, BudgetExceeded
from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, StepOutput
from loopy_runtime.harness.claude_code import ClaudeCodeHarness
from loopy_runtime.manifest_model import BudgetSpec, RefSpec, SandboxSpec, StepSpec, load_manifest
from loopy_runtime.render import TemplateRenderer
from loopy_runtime.retry import ExponentialBackoffRetry
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import EnvFileSecretsResolver, StaticSecretsResolver

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def _event(**fields):
    return Event(name="E", fields=fields, id="x", emitted_at=datetime.now(UTC))


def test_renderer_substitutes_event_and_step_refs():
    step = StepSpec(
        id="w/s",
        body="issue {{ event.issue_id }} fixed by {{ fix.pr_url }}",
        refs=[
            RefSpec(producer="event", field="issue_id", raw="{{ event.issue_id }}"),
            RefSpec(producer="fix", field="pr_url", raw="{{ fix.pr_url }}"),
        ],
    )
    rendered = TemplateRenderer().render(
        step,
        _event(issue_id="ISS-9"),
        {"fix": StepOutput(fields={"pr_url": "https://pr/1"})},
    )
    assert rendered == "issue ISS-9 fixed by https://pr/1"


def test_budget_spend_trips():
    budget = BudgetSpec(spend={"usd": 1})
    BudgetEnforcer.check_spend(budget, 0.5)  # under — ok
    with pytest.raises(BudgetExceeded):
        BudgetEnforcer.check_spend(budget, 2.0)


def test_budget_wall_clock_seconds():
    assert BudgetEnforcer.wall_clock_seconds(BudgetSpec(wall_clock=2)) == 120
    assert BudgetEnforcer.wall_clock_seconds(None) is None


def test_retry_gives_up_at_max_attempts():
    policy = ExponentialBackoffRetry(max_attempts=3)
    assert policy.next_backoff(0, Exception()) is not None
    assert policy.next_backoff(1, Exception()) is not None
    assert policy.next_backoff(2, Exception()) is None


def test_env_file_resolver_parses(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "x.env").write_text("ANTHROPIC_API_KEY=sk-test\n# c\nFOO=bar\n")
    env = EnvFileSecretsResolver(tmp_path).resolve(
        "default", SandboxSpec(env_file=["secrets/x.env"])
    )
    assert env == {"ANTHROPIC_API_KEY": "sk-test", "FOO": "bar"}


def test_env_file_resolver_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        EnvFileSecretsResolver(tmp_path).resolve("default", SandboxSpec(env_file=["nope.env"]))


def test_env_file_resolver_rejects_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        EnvFileSecretsResolver(tmp_path).resolve("default", SandboxSpec(env_file=["../escape.env"]))


def test_runtime_missing_model_key_errors():
    # ClaudeCodeHarness requires ANTHROPIC_API_KEY; StaticSecretsResolver supplies none.
    m = load_manifest(GOLDEN)
    rt = InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    event = Event(name="Incident", fields={}, id="x", emitted_at=datetime.now(UTC))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(rt.trigger(event))
