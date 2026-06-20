"""Runtime side of SCM output verification (#19).

The engine, when a token provider is configured, cross-checks a completed step's claimed
`pr_url` against GitHub and annotates the run (`runtime.verifications` + a history entry)
instead of reporting an unverified URL as a clean green. The GitHub `GET` is stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import (
    Event,
    ExecResult,
    StepContext,
    StepOutput,
    StepResult,
    ToolchainLayer,
)
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.scm import github_app
from loopy_runtime.secrets import StaticSecretsResolver


class _PRHarness:
    """A harness that returns a fixed `pr_url` output — stands in for an agent that claims
    to have opened a PR, so the runtime's post-step verification has something to check."""

    def __init__(self, pr_url: str):
        self._pr_url = pr_url

    def required_keys(self, agent):
        return set()

    def missing_keys(self, agent, env):
        return set()

    def toolchain(self, agent):
        return ToolchainLayer()

    def required_tools(self, agent):
        return set()

    async def run(self, step, ctx: StepContext, sandbox) -> StepResult:
        return StepResult(output=StepOutput({"pr_url": self._pr_url, "summary": "did it"}))


class _NoopSandbox:
    id = "s"

    async def exec(self, cmd):
        return ExecResult(0, "", "")

    async def release(self):
        return None


class _NoopProvider:
    async def acquire(self, spec, secrets):
        return _NoopSandbox()


class _FakeTokens:
    async def token_env(self, spec):
        return {"GITHUB_TOKEN": "ghs_tok"}


def _manifest() -> Manifest:
    step = {
        "id": "wf/open",
        "trigger": {"kind": "event", "event": "Go"},
        "after": [],
        "agent": None,
        "output": {"pr_url": {"type": "string"}, "summary": {"type": "string"}},
        "emits": [],
        "budget": None,
        "body": "open a PR",
        "refs": [],
    }
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Go": {"fields": {}}}},
            "workflows": {"wf": {"entry": "open", "steps": {"open": step}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _run(runtime: InMemoryRuntime):
    event = Event(name="Go", fields={}, id="t", emitted_at=datetime.now(UTC))
    return asyncio.run(runtime.trigger(event))


def _build(pr_url: str, *, tokens) -> InMemoryRuntime:
    return InMemoryRuntime(
        _manifest(),
        harness=_PRHarness(pr_url),
        sandboxes=_NoopProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
        tokens=tokens,
    )


def test_confirmed_pr_records_verified_and_no_unverified(monkeypatch):
    monkeypatch.setattr(github_app, "get_pull_request", lambda *a, **k: {"number": 7})
    rt = _build("https://github.com/o/r/pull/7", tokens=_FakeTokens())
    run_id = _run(rt)

    assert [v.status for v in rt.verifications] == ["confirmed"]
    history = asyncio.run(rt.state.history(run_id))
    assert any(e.kind == "scm_verified" and e.step_id == "wf/open" for e in history)
    # A completed run, not failed — verification annotates, it doesn't enforce.
    assert rt._runs[run_id].state == "completed"


def test_fabricated_pr_url_is_recorded_as_not_found(monkeypatch):
    def raise_404(*a, **k):
        raise github_app.GitHubAPIError(404, "Not Found")

    monkeypatch.setattr(github_app, "get_pull_request", raise_404)
    rt = _build("https://github.com/o/r/pull/999", tokens=_FakeTokens())
    run_id = _run(rt)

    assert [v.status for v in rt.verifications] == ["not_found"]
    history = asyncio.run(rt.state.history(run_id))
    entry = next(e for e in history if e.kind == "scm_unverified")
    assert entry.payload["status"] == "not_found"
    assert entry.payload["url"] == "https://github.com/o/r/pull/999"
    # The run still completed — emits/cascade semantics are unchanged.
    assert rt._runs[run_id].state == "completed"


def test_malformed_pr_url_skips_the_api(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("API must not be called for a malformed URL")

    monkeypatch.setattr(github_app, "get_pull_request", boom)
    rt = _build("https://example.com/not-a-pr", tokens=_FakeTokens())
    _run(rt)
    assert [v.status for v in rt.verifications] == ["malformed"]


def test_no_token_provider_skips_verification(monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("API must not be called without a token provider")

    monkeypatch.setattr(github_app, "get_pull_request", boom)
    rt = _build("https://github.com/o/r/pull/7", tokens=None)
    _run(rt)
    assert rt.verifications == []


def test_token_unavailable_at_verification_does_not_fail_the_run(monkeypatch):
    # The step's own token injection succeeds (first call); a later refresh for verification
    # fails. The run already ran, so verification is skipped — never manufacturing a failure.
    class _FlakyTokens:
        def __init__(self):
            self.calls = 0

        async def token_env(self, spec):
            self.calls += 1
            if self.calls == 1:
                return {"GITHUB_TOKEN": "ghs_tok"}
            raise RuntimeError("refresh exploded")

    monkeypatch.setattr(github_app, "get_pull_request", lambda *a, **k: {"number": 7})
    rt = _build("https://github.com/o/r/pull/7", tokens=_FlakyTokens())
    run_id = _run(rt)
    assert rt.verifications == []
    assert rt._runs[run_id].state == "completed"
