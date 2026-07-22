"""Tenki provider: create params, setup replay, exec mapping, release — fake client (offline)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.factory import make_sandbox_provider
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.sandbox.tenki import TenkiSandbox, TenkiSandboxProvider


@dataclass
class FakeCommandResult:
    exit_code: int = 0
    stdout_text: str = "output"
    stderr_text: str = ""


@dataclass
class FakeProject:
    id: str = "proj-1"


@dataclass
class FakeWorkspace:
    id: str = "ws-1"
    projects: tuple = (FakeProject(),)


@dataclass
class FakeIdentity:
    workspaces: tuple = (FakeWorkspace(),)


class FakeAsyncSandbox:
    def __init__(self, fail_on: str | None = None):
        self.id = "tenki-1"
        self.execs: list[tuple[tuple[str, ...], str | None]] = []
        self.terminated = False
        self._fail_on = fail_on  # substring of a command that should return exit 1

    async def exec(self, *argv, cwd=None, env=None, **kw) -> FakeCommandResult:
        self.execs.append((argv, cwd))
        if self._fail_on and any(self._fail_on in a for a in argv):
            return FakeCommandResult(exit_code=1, stdout_text="", stderr_text="boom")
        return FakeCommandResult()

    async def terminate(self) -> None:
        self.terminated = True


class FakeAsyncClient:
    def __init__(self, fail_on: str | None = None):
        self.create_kwargs: list[dict] = []
        self.closed = False
        self.who_am_i_calls = 0
        self.sandbox = FakeAsyncSandbox(fail_on=fail_on)

    async def who_am_i(self) -> FakeIdentity:
        self.who_am_i_calls += 1
        return FakeIdentity()

    async def create(self, **kwargs) -> FakeAsyncSandbox:
        self.create_kwargs.append(kwargs)
        return self.sandbox

    async def aclose(self) -> None:
        self.closed = True


def test_acquire_uses_default_image_and_injects_secrets():
    # tenki's registry can't resolve a public docker ref, so we create from the default
    # image (no image= kwarg) and replay layers; secrets are baked into the sandbox env.
    fake = FakeAsyncClient()
    provider = TenkiSandboxProvider(client=fake)
    spec = SandboxSpec(provider="tenki", image={"base": "python:3.12-slim"})
    asyncio.run(provider.acquire(spec, {"OPENAI_API_KEY": "sk-test"}))
    kwargs = fake.create_kwargs[0]
    assert "image" not in kwargs  # default image for now (base mapping is Option B)
    assert kwargs["env"] == {"OPENAI_API_KEY": "sk-test"}


def test_acquire_resolves_project_from_account(monkeypatch):
    monkeypatch.delenv("TENKI_PROJECT_ID", raising=False)
    fake = FakeAsyncClient()
    provider = TenkiSandboxProvider(client=fake)
    spec = SandboxSpec(provider="tenki", image={"base": "x"})
    asyncio.run(provider.acquire(spec, {}))
    kwargs = fake.create_kwargs[0]
    assert kwargs["project_id"] == "proj-1" and kwargs["workspace_id"] == "ws-1"
    # A second acquire reuses the cached target: no repeat who_am_i round-trip.
    asyncio.run(provider.acquire(spec, {}))
    assert fake.who_am_i_calls == 1


def test_env_override_wins_and_skips_who_am_i(monkeypatch):
    monkeypatch.setenv("TENKI_PROJECT_ID", "proj-env")
    monkeypatch.setenv("TENKI_WORKSPACE_ID", "ws-env")
    fake = FakeAsyncClient()
    asyncio.run(TenkiSandboxProvider(client=fake).acquire(SandboxSpec(provider="tenki"), {}))
    kwargs = fake.create_kwargs[0]
    assert (kwargs["project_id"], kwargs["workspace_id"]) == ("proj-env", "ws-env")
    assert fake.who_am_i_calls == 0  # explicit override means no account lookup


def test_acquire_replays_layers_sudo_only_for_system_packages():
    fake = FakeAsyncClient()
    spec = SandboxSpec(
        provider="tenki",
        image={"base": "x", "apt": ["git", "curl"], "run": ["npm install -g opencode-ai"]},
    )
    asyncio.run(TenkiSandboxProvider(client=fake).acquire(spec, {}))
    ran = [argv for argv, _cwd in fake.sandbox.execs]
    # No forced workdir/mkdir — tenki's default image runs in its own writable home.
    assert not any("mkdir" in " ".join(a) for a in ran)
    # System-package step is sudo-prefixed (non-root image); user-space step is NOT.
    apt = next(a for a in ran if any("apt-get install -y git curl" in p for p in a))
    npm = next(a for a in ran if any("npm install -g opencode-ai" in p for p in a))
    assert apt[0] == "sudo"
    assert npm[0] == "sh"


def test_acquire_releases_and_raises_when_setup_fails():
    fake = FakeAsyncClient(fail_on="apt-get")
    spec = SandboxSpec(provider="tenki", image={"base": "x", "apt": ["git"]})
    with pytest.raises(RuntimeError, match="image setup failed"):
        asyncio.run(TenkiSandboxProvider(client=fake).acquire(spec, {}))
    assert fake.sandbox.terminated is True  # half-provisioned sandbox is torn down


def test_exec_passes_argv_with_cwd_and_maps_result():
    fake = FakeAsyncClient()
    sandbox = TenkiSandbox(fake, fake.sandbox, "/workspace")
    result = asyncio.run(sandbox.exec(["echo", "hi there"]))
    argv, cwd = fake.sandbox.execs[-1]
    assert argv == ("echo", "hi there")  # real argv, no shell re-parsing
    assert cwd == "/workspace"
    assert (result.exit_code, result.stdout, result.stderr) == (0, "output", "")


def test_exec_folds_diagnostics_when_failure_has_no_stderr():
    @dataclass
    class DiagResult:
        exit_code: int = 137
        stdout_text: str = ""
        stderr_text: str = ""
        reason: str = "OOMKilled"
        signal: int = 9
        errno: int = 0  # falsy is omitted

    class DiagSandbox:
        id = "diag"

        async def exec(self, *a, **k):
            return DiagResult()

    res = asyncio.run(TenkiSandbox(None, DiagSandbox()).exec(["boom"]))
    assert res.exit_code == 137
    assert "reason=OOMKilled" in res.stderr and "signal=9" in res.stderr
    assert "errno" not in res.stderr  # zero errno dropped


def test_exec_keeps_both_stderr_and_diagnostics():
    @dataclass
    class DiagResult:
        exit_code: int = 1
        stdout_text: str = ""
        stderr_text: str = "real error output"
        reason: str = "Killed"
        signal: int = 0
        errno: int = 0

    class DiagSandbox:
        id = "diag"

        async def exec(self, *a, **k):
            return DiagResult()

    res = asyncio.run(TenkiSandbox(None, DiagSandbox()).exec(["boom"]))
    assert "real error output" in res.stderr  # original stderr kept
    assert "reason=Killed" in res.stderr  # diagnostics appended, not replacing it


def test_release_terminates_sandbox():
    fake = FakeAsyncClient()
    sandbox = TenkiSandbox(fake, fake.sandbox, "/workspace")
    asyncio.run(sandbox.release())
    assert fake.sandbox.terminated is True


def test_factory_selects_tenki_provider():
    assert isinstance(make_sandbox_provider("tenki").inner, TenkiSandboxProvider)
    assert isinstance(make_sandbox_provider("local").inner, LocalSandboxProvider)
    with pytest.raises(ValueError, match="unknown sandbox provider"):
        make_sandbox_provider("nope")


def test_aclose_closes_injected_client_and_is_idempotent():
    fake = FakeAsyncClient()
    provider = TenkiSandboxProvider(client=fake)
    asyncio.run(provider.aclose())
    assert fake.closed is True
    asyncio.run(provider.aclose())  # idempotent when already closed


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("TENKI_API_KEY", raising=False)
    monkeypatch.delenv("TENKI_AUTH_TOKEN", raising=False)
    provider = TenkiSandboxProvider()  # no injected client → must build one
    with pytest.raises(RuntimeError, match="TENKI_API_KEY is not set"):
        asyncio.run(provider.acquire(SandboxSpec(provider="tenki", image={"base": "x"}), {}))
