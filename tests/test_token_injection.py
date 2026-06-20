"""Token injection: mint scoped GitHub installation tokens into the sandbox env.

Covers the `GitHubAppTokenProvider` (mint/cache/scope/install-resolution) and the
runtime seam that merges its env into a sandbox's secrets. No live network — the
github_app API calls are stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event, ExecResult
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.scm import github_app
from loopy_runtime.scm.github_app import AppCredentials
from loopy_runtime.scm.token_provider import GitHubAppTokenProvider, git_credential_env
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness

CREDS = AppCredentials(app_id="1", private_key_pem="PEM")


def _future(minutes: int = 60) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


# --- git credential wiring --------------------------------------------------


def test_git_credential_env_keeps_token_out_of_config_value():
    env = git_credential_env("ghs_secret")
    assert env["GITHUB_TOKEN"] == "ghs_secret"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    # The token rides GITHUB_TOKEN; the helper only references it by name.
    assert "ghs_secret" not in env["GIT_CONFIG_VALUE_0"]
    assert "${GITHUB_TOKEN}" in env["GIT_CONFIG_VALUE_0"]


# --- minting / scoping ------------------------------------------------------


def test_token_env_mints_and_wires_git(monkeypatch):
    monkeypatch.setattr(
        github_app,
        "mint_installation_token",
        lambda *a, **k: {"token": "ghs_x", "expires_at": _future()},
    )
    provider = GitHubAppTokenProvider(CREDS, installation_id=42)
    env = asyncio.run(provider.token_env(spec=None))
    assert env["GITHUB_TOKEN"] == "ghs_x"
    assert env["GIT_CONFIG_COUNT"] == "1"


def test_token_scoped_to_repositories(monkeypatch):
    captured = {}

    def fake_mint(creds, installation_id, *, repositories=None, permissions=None, base_url=None):
        captured["installation_id"] = installation_id
        captured["repositories"] = repositories
        return {"token": "t", "expires_at": _future()}

    monkeypatch.setattr(github_app, "mint_installation_token", fake_mint)
    provider = GitHubAppTokenProvider(CREDS, installation_id=7, repositories=["api", "web"])
    asyncio.run(provider.token_env(spec=None))
    assert captured["installation_id"] == 7
    assert captured["repositories"] == ["api", "web"]


# --- caching ----------------------------------------------------------------


def test_token_is_cached_until_near_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_mint(*a, **k):
        calls["n"] += 1
        return {"token": f"t{calls['n']}", "expires_at": _future(60)}

    monkeypatch.setattr(github_app, "mint_installation_token", fake_mint)
    provider = GitHubAppTokenProvider(CREDS, installation_id=1)
    first = asyncio.run(provider.token_env(spec=None))["GITHUB_TOKEN"]
    second = asyncio.run(provider.token_env(spec=None))["GITHUB_TOKEN"]
    assert first == second == "t1"  # one mint shared
    assert calls["n"] == 1


def test_token_refreshes_when_expired(monkeypatch):
    calls = {"n": 0}

    def fake_mint(*a, **k):
        calls["n"] += 1
        # Already past → outside the safety margin → must re-mint next time.
        return {"token": f"t{calls['n']}", "expires_at": _future(-5)}

    monkeypatch.setattr(github_app, "mint_installation_token", fake_mint)
    provider = GitHubAppTokenProvider(CREDS, installation_id=1)
    first = asyncio.run(provider.token_env(spec=None))["GITHUB_TOKEN"]
    second = asyncio.run(provider.token_env(spec=None))["GITHUB_TOKEN"]
    assert (first, second) == ("t1", "t2")
    assert calls["n"] == 2


def test_bad_expiry_falls_back_to_finite_lifetime(monkeypatch):
    monkeypatch.setattr(
        github_app,
        "mint_installation_token",
        lambda *a, **k: {"token": "t", "expires_at": "garbage"},
    )
    provider = GitHubAppTokenProvider(CREDS, installation_id=1)
    # Should not raise; the fallback lifetime keeps the token cached.
    assert asyncio.run(provider.token_env(spec=None))["GITHUB_TOKEN"] == "t"


# --- installation resolution ------------------------------------------------


def test_resolves_sole_installation(monkeypatch):
    monkeypatch.setattr(github_app, "list_installations", lambda *a, **k: [{"id": 99}])
    seen = {}

    def fake_mint(creds, installation_id, **k):
        seen["id"] = installation_id
        return {"token": "t", "expires_at": _future()}

    monkeypatch.setattr(github_app, "mint_installation_token", fake_mint)
    provider = GitHubAppTokenProvider(CREDS)  # no explicit id → discover
    asyncio.run(provider.token_env(spec=None))
    assert seen["id"] == 99


def test_no_installation_raises(monkeypatch):
    monkeypatch.setattr(github_app, "list_installations", lambda *a, **k: [])
    provider = GitHubAppTokenProvider(CREDS)
    with pytest.raises(github_app.GitHubAppError, match="no installations"):
        asyncio.run(provider.token_env(spec=None))


def test_multiple_installations_raise(monkeypatch):
    monkeypatch.setattr(github_app, "list_installations", lambda *a, **k: [{"id": 1}, {"id": 2}])
    provider = GitHubAppTokenProvider(CREDS)
    with pytest.raises(github_app.GitHubAppError, match="multiple installations"):
        asyncio.run(provider.token_env(spec=None))


# --- preflight: prove creds mint before any step runs -----------------------


def test_preflight_mints_and_warms_cache(monkeypatch):
    calls = []

    def fake_mint(*a, **k):
        calls.append(1)
        return {"token": "ghs_p", "expires_at": _future()}

    monkeypatch.setattr(github_app, "mint_installation_token", fake_mint)
    provider = GitHubAppTokenProvider(CREDS, installation_id=7)
    asyncio.run(provider.preflight())
    # The preflight mint is cached, so a follow-up token_env reuses it (no second mint).
    env = asyncio.run(provider.token_env(spec=None))
    assert env["GITHUB_TOKEN"] == "ghs_p"
    assert len(calls) == 1


def test_preflight_raises_when_no_installation(monkeypatch):
    monkeypatch.setattr(github_app, "list_installations", lambda *a, **k: [])
    provider = GitHubAppTokenProvider(CREDS)  # no explicit id → must resolve
    with pytest.raises(github_app.GitHubAppError, match="no installations"):
        asyncio.run(provider.preflight())


# --- runtime seam: env reaches the sandbox ----------------------------------


class _CapturingProvider:
    """A SandboxProvider that records the secrets handed to it."""

    def __init__(self):
        self.secrets: dict[str, str] | None = None

    async def acquire(self, spec, secrets):
        self.secrets = dict(secrets)
        return _NoopSandbox()


class _NoopSandbox:
    id = "cap"

    async def exec(self, cmd):
        return ExecResult(0, "", "")

    async def release(self):
        return None


class _FakeTokens:
    async def token_env(self, spec):
        return {"GITHUB_TOKEN": "ghs_injected", "GIT_CONFIG_COUNT": "1"}


def _one_step_manifest() -> Manifest:
    step = {
        "id": "wf/run",
        "trigger": {"kind": "event", "event": "Go"},
        "after": [],
        "agent": None,
        "output": {},
        "emits": [],
        "budget": None,
        "body": "do",
        "refs": [],
    }
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Go": {"fields": {}}}},
            "workflows": {"wf": {"entry": "run", "steps": {"run": step}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def test_runtime_injects_token_env_into_sandbox():
    provider = _CapturingProvider()
    rt = InMemoryRuntime(
        _one_step_manifest(),
        harness=StubAgentHarness({}),
        sandboxes=provider,
        secrets=StaticSecretsResolver({"PRELOADED": "1"}),
        bus=InProcessEventBus(),
        tokens=_FakeTokens(),
    )
    asyncio.run(rt.trigger(Event(name="Go", fields={}, id="t", emitted_at=datetime.now(UTC))))
    assert provider.secrets["GITHUB_TOKEN"] == "ghs_injected"  # token injected
    assert provider.secrets["PRELOADED"] == "1"  # alongside resolved secrets


def test_runtime_injects_run_id_into_sandbox():
    # LOOPY_RUN_ID is injected for traceability so the agent can stamp the run onto a PR/commit.
    provider = _CapturingProvider()
    rt = InMemoryRuntime(
        _one_step_manifest(),
        harness=StubAgentHarness({}),
        sandboxes=provider,
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )
    run_id = asyncio.run(
        rt.trigger(Event(name="Go", fields={}, id="t", emitted_at=datetime.now(UTC)))
    )
    assert provider.secrets["LOOPY_RUN_ID"] == run_id


def test_env_file_cannot_shadow_run_id():
    # Engine-owned: a sandbox env_file value for LOOPY_RUN_ID must not win over the real run id.
    provider = _CapturingProvider()
    rt = InMemoryRuntime(
        _one_step_manifest(),
        harness=StubAgentHarness({}),
        sandboxes=provider,
        secrets=StaticSecretsResolver({"LOOPY_RUN_ID": "forged"}),
        bus=InProcessEventBus(),
    )
    run_id = asyncio.run(
        rt.trigger(Event(name="Go", fields={}, id="t", emitted_at=datetime.now(UTC)))
    )
    assert provider.secrets["LOOPY_RUN_ID"] == run_id != "forged"


def test_runtime_without_tokens_is_unchanged():
    provider = _CapturingProvider()
    rt = InMemoryRuntime(
        _one_step_manifest(),
        harness=StubAgentHarness({}),
        sandboxes=provider,
        secrets=StaticSecretsResolver({"PRELOADED": "1"}),
        bus=InProcessEventBus(),
    )
    asyncio.run(rt.trigger(Event(name="Go", fields={}, id="t", emitted_at=datetime.now(UTC))))
    assert "GITHUB_TOKEN" not in provider.secrets  # no provider → no injection
