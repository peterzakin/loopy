"""Docker provider: image planning, container start + env injection, exec/release —
via a fake `run` (no real docker daemon, offline)."""

from __future__ import annotations

import asyncio

import pytest

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.docker import (
    DockerSandbox,
    DockerSandboxProvider,
    plan_docker_image,
)
from loopy_runtime.sandbox.factory import make_sandbox_provider
from loopy_runtime.sandbox.local import LocalSandboxProvider


class FakeRunner:
    """Records every `docker` argv and returns canned results (container id for `run`)."""

    def __init__(self, container_id: str = "c0ffee"):
        self.calls: list[list[str]] = []
        self._container_id = container_id

    async def __call__(self, argv: list[str]) -> ExecResult:
        self.calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            return ExecResult(0, self._container_id + "\n", "")
        return ExecResult(0, "ok", "")


# --- image planning (pure) ---------------------------------------------------


def test_plan_debian_slim_maps_to_python_image():
    plan = plan_docker_image({"debian_slim": "3.12"})
    assert plan.base == "python:3.12-slim"


def test_plan_base_and_snapshot_pass_through():
    assert plan_docker_image({"base": "node:20-slim"}).base == "node:20-slim"
    assert plan_docker_image({"snapshot": "myorg/snap:1"}).base == "myorg/snap:1"


def test_plan_defaults_and_workdir():
    plan = plan_docker_image(None)
    assert plan.base == "python:3.12-slim"
    assert plan.workdir == "/workspace"
    assert plan_docker_image({"workdir": "/home/app"}).workdir == "/home/app"


def test_plan_apt_becomes_setup_command():
    plan = plan_docker_image({"debian_slim": "3.12", "apt": ["git", "curl"]})
    assert any("apt-get install -y git curl" in c for c in plan.setup)


def test_plan_dockerfile_not_supported():
    with pytest.raises(ValueError, match="Dockerfile"):
        plan_docker_image({"dockerfile": "Dockerfile"})


def test_plan_reuses_validation_for_secret_env():
    with pytest.raises(ValueError, match="secrets"):
        plan_docker_image({"env": {"ANTHROPIC_API_KEY": "sk"}})


# --- provider: start, env injection, setup -----------------------------------


def test_acquire_starts_container_injects_env_and_runs_setup():
    runner = FakeRunner()
    provider = DockerSandboxProvider(run=runner)
    spec = SandboxSpec(image={"debian_slim": "3.12", "apt": ["git"], "workdir": "/work"})
    sandbox = asyncio.run(provider.acquire(spec, {"GITHUB_TOKEN": "ghs_x"}))

    run_cmd = runner.calls[0]
    assert run_cmd[:4] == ["docker", "run", "-d", "-w"]
    assert "/work" in run_cmd
    assert "-e" in run_cmd and "GITHUB_TOKEN=ghs_x" in run_cmd  # secret injected as env
    assert run_cmd[-4:] == ["python:3.12-slim", "tail", "-f", "/dev/null"]  # base + keep-alive
    assert sandbox.id == "c0ffee"
    # apt setup replayed inside the container.
    assert any("apt-get install -y git" in " ".join(c) for c in runner.calls)


def test_secret_overrides_image_env():
    runner = FakeRunner()
    spec = SandboxSpec(image={"base": "python:3.12-slim", "env": {"FOO": "from-image"}})
    asyncio.run(DockerSandboxProvider(run=runner).acquire(spec, {"FOO": "from-secret"}))
    run_cmd = runner.calls[0]
    assert "FOO=from-secret" in run_cmd
    assert "FOO=from-image" not in run_cmd


def test_acquire_raises_when_setup_fails():
    class FailingSetup(FakeRunner):
        async def __call__(self, argv):
            self.calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                return ExecResult(0, "cid\n", "")
            if "apt-get" in " ".join(argv):
                return ExecResult(1, "", "E: package not found")
            return ExecResult(0, "", "")

    runner = FailingSetup()
    spec = SandboxSpec(image={"debian_slim": "3.12", "apt": ["nope"]})
    with pytest.raises(RuntimeError, match="image setup failed"):
        asyncio.run(DockerSandboxProvider(run=runner).acquire(spec, {}))
    assert any(c[:3] == ["docker", "rm", "-f"] for c in runner.calls)  # cleaned up


# --- sandbox: exec / release -------------------------------------------------


def test_exec_passes_argv_verbatim_in_workdir():
    runner = FakeRunner()
    sandbox = DockerSandbox("cid", "/work", runner)
    asyncio.run(sandbox.exec(["claude", "-p", "do it"]))
    assert runner.calls[0] == ["docker", "exec", "-w", "/work", "cid", "claude", "-p", "do it"]


def test_release_force_removes_container():
    runner = FakeRunner()
    asyncio.run(DockerSandbox("cid", "/work", runner).release())
    assert runner.calls[0] == ["docker", "rm", "-f", "cid"]


# --- factory -----------------------------------------------------------------


def test_factory_selects_docker_provider():
    assert isinstance(make_sandbox_provider("docker").inner, DockerSandboxProvider)
    assert isinstance(make_sandbox_provider("local").inner, LocalSandboxProvider)
