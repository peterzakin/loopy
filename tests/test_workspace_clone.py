"""Repo cloning into the sandbox workspace: the shared helper + the factory decorator."""

from __future__ import annotations

import asyncio

import pytest

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import RepoSpec, SandboxSpec
from loopy_runtime.sandbox.factory import _RepoCloningProvider
from loopy_runtime.sandbox.workspace import (
    normalize_repo_url,
    provision_workspace,
    repo_dest,
)


class FakeSandbox:
    """Records every exec argv; returns a configurable exit code per command prefix."""

    def __init__(self, fail_on: str | None = None):
        self.id = "fake"
        self.calls: list[list[str]] = []
        self.released = False
        self._fail_on = fail_on

    async def exec(self, cmd: list[str]) -> ExecResult:
        self.calls.append(cmd)
        if self._fail_on and self._fail_on in " ".join(cmd):
            return ExecResult(exit_code=1, stdout="", stderr="boom")
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    async def release(self) -> None:
        self.released = True


# ── URL + destination normalization ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("acme/runbooks", "https://github.com/acme/runbooks.git"),
        ("acme/runbooks.git", "https://github.com/acme/runbooks.git"),
        ("https://github.com/acme/runbooks.git", "https://github.com/acme/runbooks.git"),
        ("git@github.com:acme/runbooks.git", "git@github.com:acme/runbooks.git"),
    ],
)
def test_normalize_repo_url(url, expected):
    assert normalize_repo_url(url) == expected


def test_repo_dest_prefers_path_then_repo_name():
    assert repo_dest(RepoSpec(url="acme/runbooks")) == "runbooks"
    assert repo_dest(RepoSpec(url="acme/runbooks", path="vendor/rb")) == "vendor/rb"
    assert repo_dest(RepoSpec(url="https://github.com/acme/runbooks.git")) == "runbooks"


# ── Clone command shape ─────────────────────────────────────────────────────────


def test_shallow_clone_default_is_relative_and_depth_one():
    sandbox = FakeSandbox()
    asyncio.run(provision_workspace(sandbox, [RepoSpec(url="acme/runbooks")]))
    assert sandbox.calls == [
        ["git", "clone", "--depth", "1", "https://github.com/acme/runbooks.git", "runbooks"]
    ]


def test_shallow_clone_with_ref_uses_branch_flag():
    sandbox = FakeSandbox()
    asyncio.run(provision_workspace(sandbox, [RepoSpec(url="acme/rb", ref="dev", path="rb")]))
    assert sandbox.calls == [
        ["git", "clone", "--depth", "1", "--branch", "dev",
         "https://github.com/acme/rb.git", "rb"]
    ]


def test_full_clone_with_ref_checks_out_after_clone():
    sandbox = FakeSandbox()
    asyncio.run(provision_workspace(sandbox, [RepoSpec(url="acme/rb", ref="abc123", depth=None)]))
    assert sandbox.calls == [
        ["git", "clone", "https://github.com/acme/rb.git", "rb"],
        ["git", "-C", "rb", "checkout", "abc123"],
    ]


def test_multiple_repos_clone_in_order():
    sandbox = FakeSandbox()
    asyncio.run(
        provision_workspace(sandbox, [RepoSpec(url="a/one"), RepoSpec(url="b/two")])
    )
    assert [c[-1] for c in sandbox.calls] == ["one", "two"]


def test_failed_clone_raises_with_detail():
    sandbox = FakeSandbox(fail_on="clone")
    with pytest.raises(RuntimeError, match="clone 'acme/rb' failed: boom"):
        asyncio.run(provision_workspace(sandbox, [RepoSpec(url="acme/rb")]))


# ── Factory decorator wiring ─────────────────────────────────────────────────────


class FakeProvider:
    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.acquired = False

    async def acquire(self, spec, secrets):
        self.acquired = True
        return self._sandbox


def test_decorator_clones_declared_repos_after_acquire():
    sandbox = FakeSandbox()
    provider = _RepoCloningProvider(FakeProvider(sandbox))
    spec = SandboxSpec(repos=[RepoSpec(url="acme/runbooks")])
    out = asyncio.run(provider.acquire(spec, {}))
    assert out is sandbox
    assert sandbox.calls[0][:2] == ["git", "clone"]


def test_decorator_no_repos_is_a_noop():
    sandbox = FakeSandbox()
    provider = _RepoCloningProvider(FakeProvider(sandbox))
    asyncio.run(provider.acquire(SandboxSpec(), {}))
    assert sandbox.calls == []


def test_decorator_releases_sandbox_when_clone_fails():
    sandbox = FakeSandbox(fail_on="clone")
    provider = _RepoCloningProvider(FakeProvider(sandbox))
    spec = SandboxSpec(repos=[RepoSpec(url="acme/rb")])
    with pytest.raises(RuntimeError):
        asyncio.run(provider.acquire(spec, {}))
    assert sandbox.released is True
