"""Islo provider: image planning, SDK param mapping, exec polling, release."""

from __future__ import annotations

import asyncio

import pytest

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.factory import make_sandbox_provider
from loopy_runtime.sandbox.islo import IsloSandbox, IsloSandboxProvider, plan_islo_image
from loopy_runtime.sandbox.local import LocalSandboxProvider


class FakeObject:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeSandboxes:
    def __init__(self):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.exec_calls: list[dict] = []
        self.results: list[FakeObject] = []

    async def create_sandbox(self, **kwargs):
        self.created.append(kwargs)
        return FakeObject(name=kwargs["name"], id="sb-1")

    async def delete_sandbox(self, *, sandbox_name: str):
        self.deleted.append(sandbox_name)

    async def exec_in_sandbox(self, **kwargs):
        self.exec_calls.append(kwargs)
        return FakeObject(exec_id=f"exec-{len(self.exec_calls)}")

    async def get_exec_result(self, *, sandbox_name: str, exec_id: str):
        if self.results:
            return self.results.pop(0)
        return FakeObject(exec_id=exec_id, exit_code=0, status="completed", stdout="ok", stderr="")


class FakeIslo:
    def __init__(self):
        self.sandboxes = FakeSandboxes()
        self.closed = False

    async def aclose(self):
        self.closed = True


def test_plan_image_defaults_to_islo_ubuntu():
    plan = plan_islo_image(None)
    assert plan.image == "docker.io/library/ubuntu:26.04"
    assert plan.workdir == "/workspace/loopy"
    assert plan.user == "root"


def test_plan_image_maps_base_and_layers():
    plan = plan_islo_image(
        {
            "base": "node:22-slim",
            "workdir": "/work",
            "user": "islo",
            "env": {"FOO": "bar"},
            "apt": ["git", "curl"],
            "pip": ["pytest"],
            "run": ["echo ready"],
            "vcpus": 2,
            "memory_mb": 4096,
            "disk_gb": 20,
        }
    )
    assert plan.image == "node:22-slim"
    assert plan.workdir == "/work"
    assert plan.user == "islo"
    assert plan.env == {"FOO": "bar"}
    assert any("apt-get install -y git curl" in command for command in plan.setup)
    assert any("python -m pip install pytest" in command for command in plan.setup)
    assert plan.setup[-1] == "echo ready"
    assert plan.vcpus == 2
    assert plan.memory_mb == 4096
    assert plan.disk_gb == 20


def test_plan_rejects_secret_env_and_unknown_keys():
    with pytest.raises(ValueError, match="secrets"):
        plan_islo_image({"env": {"ANTHROPIC_API_KEY": "sk-test"}})
    with pytest.raises(ValueError, match="unknown Islo image keys"):
        plan_islo_image({"dockerfile": "Dockerfile"})


def test_acquire_creates_sandbox_injects_env_and_runs_setup():
    fake = FakeIslo()
    provider = IsloSandboxProvider(client=fake, poll_interval=0)
    spec = SandboxSpec(
        image={
            "image": "ubuntu:24.04",
            "workdir": "/workspace/app",
            "apt": ["git"],
            "run": ["echo setup"],
        }
    )

    sandbox = asyncio.run(provider.acquire(spec, {"GITHUB_TOKEN": "ghs_x"}))

    create = fake.sandboxes.created[0]
    assert create["name"].startswith("loopy-")
    assert create["image"] == "ubuntu:24.04"
    assert create["workdir"] == "/workspace/app"
    assert create["env"] == {"GITHUB_TOKEN": "ghs_x"}
    assert sandbox.id == create["name"]
    setup_commands = [call["command"] for call in fake.sandboxes.exec_calls]
    assert ["bash", "-lc", "mkdir -p /workspace/app"] in setup_commands
    assert any(
        command[:2] == ["bash", "-lc"] and "apt-get install -y git" in command[2]
        for command in setup_commands
    )


def test_acquire_cleans_up_when_setup_fails():
    fake = FakeIslo()
    fake.sandboxes.results = [
        FakeObject(exec_id="exec-1", exit_code=7, status="completed", stdout="", stderr="nope")
    ]
    provider = IsloSandboxProvider(client=fake, poll_interval=0)

    with pytest.raises(RuntimeError, match="Islo setup failed"):
        asyncio.run(provider.acquire(SandboxSpec(image={"run": ["false"]}), {}))

    assert fake.sandboxes.deleted == [fake.sandboxes.created[0]["name"]]


def test_exec_polls_until_result_and_maps_streams():
    fake = FakeIslo()
    fake.sandboxes.results = [
        FakeObject(exec_id="exec-1", exit_code=None, status="running", stdout="", stderr=""),
        FakeObject(exec_id="exec-1", exit_code=3, status="completed", stdout="out", stderr="err"),
    ]
    sandbox = IsloSandbox(
        fake,
        FakeObject(name="loopy-test"),
        workdir="/workspace/app",
        user="islo",
        poll_interval=0,
    )

    result = asyncio.run(sandbox.exec(["echo", "hi there"]))

    assert result == ExecResult(exit_code=3, stdout="out", stderr="err")
    assert fake.sandboxes.exec_calls[0] == {
        "sandbox_name": "loopy-test",
        "command": ["echo", "hi there"],
        "workdir": "/workspace/app",
        "user": "islo",
    }


def test_release_deletes_sandbox():
    fake = FakeIslo()
    sandbox = IsloSandbox(fake, FakeObject(name="loopy-test"), workdir="/work", user="root")
    asyncio.run(sandbox.release())
    assert fake.sandboxes.deleted == ["loopy-test"]


def test_factory_selects_islo_provider():
    assert isinstance(make_sandbox_provider("local").inner, LocalSandboxProvider)
    assert isinstance(make_sandbox_provider("islo").inner, IsloSandboxProvider)


def test_aclose_closes_injected_client():
    fake = FakeIslo()
    provider = IsloSandboxProvider(client=fake)
    asyncio.run(provider.aclose())
    assert fake.closed is True


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("ISLO_API_KEY", raising=False)
    provider = IsloSandboxProvider()
    with pytest.raises(RuntimeError, match="ISLO_API_KEY is not set"):
        asyncio.run(provider.acquire(SandboxSpec(), {}))
