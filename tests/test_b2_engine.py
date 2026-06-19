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


def test_runtime_missing_model_key_records_failed_run():
    # ClaudeCodeHarness requires ANTHROPIC_API_KEY; StaticSecretsResolver supplies none.
    # The engine now isolates the failure: the run is recorded as failed (carrying the
    # error) rather than raising out of the cascade — `loopy trigger` surfaces it via
    # exit 1, and `serve()` keeps running.
    m = load_manifest(GOLDEN)
    rt = InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    event = Event(name="Incident", fields={}, id="x", emitted_at=datetime.now(UTC))
    run_id = asyncio.run(rt.trigger(event))
    status = asyncio.run(rt.status(run_id))
    assert status.state == "failed"
    assert "ANTHROPIC_API_KEY" in (status.error or "")


class _StubTokens:
    """Minimal TokenProvider stand-in: records preflight calls; raises `error` if set."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.preflight_calls = 0

    async def token_env(self, spec):
        return {}

    async def preflight(self) -> None:
        self.preflight_calls += 1
        if self.error:
            raise self.error


def _runtime(secrets, tokens=None):
    m = load_manifest(GOLDEN)
    return InMemoryRuntime(
        m,
        harness=ClaudeCodeHarness(m.registry.agents),
        sandboxes=LocalSandboxProvider(),
        secrets=secrets,
        bus=InProcessEventBus(),
        tokens=tokens,
    )


def test_preflight_raises_on_missing_provider_key():
    # Fail fast before any step runs: a sandbox that can't supply ANTHROPIC_API_KEY is a
    # PreflightError naming the agent + the missing key, not a mid-cascade failure.
    from loopy_runtime.runtime.inmemory import PreflightError

    rt = _runtime(StaticSecretsResolver({}))
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert "pre-flight failed" in str(exc.value)


def test_preflight_passes_when_keys_present():
    # The golden sandbox clones repos, so GitHub auth is required too: with the model key
    # supplied and a working token provider, preflight is a no-op (no run is instantiated).
    stub = _StubTokens()
    rt = _runtime(StaticSecretsResolver({"ANTHROPIC_API_KEY": "sk-test"}), tokens=stub)
    rt.preflight()
    assert stub.preflight_calls == 1  # one mint warms the cache for the first real step


def test_preflight_fails_when_repos_need_github_but_no_auth():
    # The golden sandbox declares `repos:` to clone, so it needs GitHub auth; with no
    # TokenProvider wired, preflight reports it up front instead of failing on first clone.
    from loopy_runtime.runtime.inmemory import PreflightError

    rt = _runtime(StaticSecretsResolver({"ANTHROPIC_API_KEY": "sk-test"}))  # tokens=None
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "GitHub auth" in str(exc.value)
    assert "loopy auth github" in str(exc.value)


def test_preflight_fails_when_token_unmintable():
    # GitHub auth is wired but can't mint (e.g. App not installed): surface the error up front.
    from loopy_runtime.runtime.inmemory import PreflightError
    from loopy_runtime.scm.github_app import GitHubAppError

    rt = _runtime(
        StaticSecretsResolver({"ANTHROPIC_API_KEY": "sk-test"}),
        tokens=_StubTokens(error=GitHubAppError("the GitHub App has no installations")),
    )
    with pytest.raises(PreflightError) as exc:
        rt.preflight()
    assert "no installations" in str(exc.value)
