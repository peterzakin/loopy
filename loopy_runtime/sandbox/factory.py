"""Select a SandboxProvider by name (lazy imports so optional providers stay optional).

Every provider is wrapped so a sandbox's declared `repos:` are cloned into the workspace
right after the compute is acquired — one place, behind the `SandboxProvider` seam, so the
runtime orchestration stays effect-free and local/docker/daytona all get the behavior.
"""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.contract import Sandbox, SandboxProvider
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.workspace import provision_workspace


class _RepoCloningProvider:
    """Wrap a provider so `spec.repos` clone into the workspace after `acquire`."""

    def __init__(self, inner: SandboxProvider):
        self.inner = inner  # the wrapped concrete provider (local/docker/daytona)

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> Sandbox:
        sandbox = await self.inner.acquire(spec, secrets)
        if spec.repos:
            try:
                await provision_workspace(sandbox, spec.repos)
            except Exception:
                # A half-provisioned workspace is useless; release the compute before failing.
                await sandbox.release()
                raise
        return sandbox

    async def aclose(self) -> None:
        """Tear down the wrapped provider, if it holds resources to release (e.g. the
        Daytona client's HTTP session). Providers without an `aclose` are a no-op."""
        inner_aclose = getattr(self.inner, "aclose", None)
        if inner_aclose is not None:
            await inner_aclose()


def make_sandbox_provider(name: str = "local"):
    if name == "local":
        from loopy_runtime.sandbox.local import LocalSandboxProvider

        provider: SandboxProvider = LocalSandboxProvider()
    elif name == "docker":
        from loopy_runtime.sandbox.docker import DockerSandboxProvider

        provider = DockerSandboxProvider()
    elif name == "daytona":
        from loopy_runtime.sandbox.daytona import DaytonaSandboxProvider

        provider = DaytonaSandboxProvider()
    else:
        raise ValueError(
            f"unknown sandbox provider {name!r}; choose 'local', 'docker', or 'daytona'"
        )
    return _RepoCloningProvider(provider)
