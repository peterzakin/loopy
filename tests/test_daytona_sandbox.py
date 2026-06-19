"""Daytona provider: param building, exec mapping, release — via a fake client (offline)."""

from __future__ import annotations

import asyncio

import pytest

from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.daytona import DaytonaSandbox, DaytonaSandboxProvider
from loopy_runtime.sandbox.factory import make_sandbox_provider
from loopy_runtime.sandbox.local import LocalSandboxProvider


class FakeResponse:
    def __init__(self, result: str, exit_code: int):
        self.result = result
        self.exit_code = exit_code


class FakeSandbox:
    def __init__(self):
        self.id = "sb-1"
        self.commands: list[str] = []
        self.process = self  # so `sandbox.process.exec` resolves here

    async def exec(self, command: str) -> FakeResponse:
        self.commands.append(command)
        return FakeResponse("output", 0)


class FakeDaytona:
    def __init__(self):
        self.created: list = []
        self.deleted: list = []
        self.closed = False
        self.sandbox = FakeSandbox()

    async def create(self, params):
        self.created.append(params)
        return self.sandbox

    async def delete(self, sandbox):
        self.deleted.append(sandbox)

    async def close(self):
        self.closed = True


def test_acquire_builds_image_and_injects_secrets():
    fake = FakeDaytona()
    provider = DaytonaSandboxProvider(client=fake)
    spec = SandboxSpec(image={"base": "python:3.12-slim"})
    asyncio.run(provider.acquire(spec, {"ANTHROPIC_API_KEY": "sk-test"}))
    params = fake.created[0]
    # image is now a built Daytona Image object (mapping is unit-tested separately).
    assert params.image is not None
    assert params.env_vars == {"ANTHROPIC_API_KEY": "sk-test"}


def test_acquire_from_snapshot_uses_snapshot_params():
    fake = FakeDaytona()
    spec = SandboxSpec(image={"snapshot": "snap-1"})
    asyncio.run(DaytonaSandboxProvider(client=fake).acquire(spec, {"K": "v"}))
    params = fake.created[0]
    assert getattr(params, "snapshot", None) == "snap-1"
    assert params.env_vars == {"K": "v"}


def test_exec_joins_argv_and_maps_result():
    fake = FakeDaytona()
    sandbox = DaytonaSandbox(fake, fake.sandbox)
    result = asyncio.run(sandbox.exec(["echo", "hi there"]))
    assert fake.sandbox.commands == ["echo 'hi there'"]  # shlex.join quoting
    assert result.exit_code == 0
    assert result.stdout == "output"


def test_release_deletes_sandbox():
    fake = FakeDaytona()
    sandbox = DaytonaSandbox(fake, fake.sandbox)
    asyncio.run(sandbox.release())
    assert fake.deleted == [fake.sandbox]


def test_factory_selects_provider():
    # The factory wraps every provider in the repo-cloning decorator; assert on the inner.
    assert isinstance(make_sandbox_provider("local").inner, LocalSandboxProvider)
    assert isinstance(make_sandbox_provider("daytona").inner, DaytonaSandboxProvider)
    with pytest.raises(ValueError, match="unknown sandbox provider"):
        make_sandbox_provider("nope")


def test_aclose_closes_injected_client_and_is_idempotent():
    fake = FakeDaytona()
    provider = DaytonaSandboxProvider(client=fake)
    asyncio.run(provider.aclose())
    assert fake.closed is True
    asyncio.run(provider.aclose())  # idempotent: no error when already closed


def test_factory_aclose_propagates_to_inner_daytona_client():
    fake = FakeDaytona()
    wrapped = make_sandbox_provider("daytona")
    wrapped.inner._client = fake  # inject so no real client is constructed
    asyncio.run(wrapped.aclose())
    assert fake.closed is True


def test_factory_aclose_is_noop_for_providers_without_aclose():
    # The local provider has no aclose; the wrapper must not blow up.
    asyncio.run(make_sandbox_provider("local").aclose())


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    provider = DaytonaSandboxProvider()  # no injected client → must build one
    with pytest.raises(RuntimeError, match="DAYTONA_API_KEY is not set"):
        asyncio.run(provider.acquire(SandboxSpec(image={"base": "x"}), {}))
