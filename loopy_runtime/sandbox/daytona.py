"""Daytona sandbox provider — run agents in Daytona cloud sandboxes (isolated
containers) behind the `SandboxProvider`/`Sandbox` Protocols.

The `daytona` SDK is an optional dependency: it's imported lazily, and a client can be
injected (so tests never hit the real service). Secrets inject as the sandbox's
`env_vars` (the sandbox is the trust boundary); `exec` maps an argv list to a shell
command and returns an `ExecResult`; `release` deletes the sandbox.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec

_DEFAULT_IMAGE = "debian:12.9"


class DaytonaSandbox:
    def __init__(self, client, sandbox):
        self._client = client
        self._sandbox = sandbox
        self.id = getattr(sandbox, "id", "daytona")

    async def exec(self, cmd: list[str]) -> ExecResult:
        # Daytona's process.exec takes a single shell command; join argv safely.
        response = await self._sandbox.process.exec(shlex.join(cmd))
        # Daytona returns combined output in `result`; there's no separate stderr.
        return ExecResult(exit_code=response.exit_code, stdout=response.result, stderr="")

    async def release(self) -> None:
        await self._client.delete(self._sandbox)


class DaytonaSandboxProvider:
    def __init__(self, client=None):
        # `client` is an AsyncDaytona (or compatible) instance; injectable for tests.
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            try:
                from daytona import AsyncDaytona
            except ImportError as exc:  # pragma: no cover - exercised via the missing-SDK test path
                raise RuntimeError(
                    "the Daytona SDK is not installed; `pip install loopy-core[daytona]`"
                ) from exc
            self._client = AsyncDaytona()  # reads DAYTONA_API_KEY / DAYTONA_API_URL
        return self._client

    @staticmethod
    def _image(spec: SandboxSpec) -> str:
        # v1: a base-image string. Full image-build mapping (apt packages, etc.) is deferred.
        return spec.image.get("image", _DEFAULT_IMAGE) if spec.image else _DEFAULT_IMAGE

    def _create_params(self, spec: SandboxSpec, secrets: Mapping[str, str]):
        from daytona import CreateSandboxFromImageParams

        return CreateSandboxFromImageParams(image=self._image(spec), env_vars=dict(secrets))

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> DaytonaSandbox:
        client = self._ensure_client()
        sandbox = await client.create(self._create_params(spec, secrets))
        return DaytonaSandbox(client, sandbox)
