"""Select a SandboxProvider by name (lazy imports so optional providers stay optional)."""

from __future__ import annotations


def make_sandbox_provider(name: str = "local"):
    if name == "local":
        from loopy_runtime.sandbox.local import LocalSandboxProvider

        return LocalSandboxProvider()
    if name == "docker":
        from loopy_runtime.sandbox.docker import DockerSandboxProvider

        return DockerSandboxProvider()
    if name == "daytona":
        from loopy_runtime.sandbox.daytona import DaytonaSandboxProvider

        return DaytonaSandboxProvider()
    raise ValueError(
        f"unknown sandbox provider {name!r}; choose 'local', 'docker', or 'daytona'"
    )
