"""Daytona sandbox provider — run agents in Daytona cloud sandboxes (isolated
containers) behind the `SandboxProvider`/`Sandbox` Protocols.

The `daytona` SDK is an optional dependency: it's imported lazily, and a client can be
injected (so tests never hit the real service). Secrets inject as the sandbox's
`env_vars` (the sandbox is the trust boundary); `exec` maps an argv list to a shell
command and returns an `ExecResult`; `release` deletes the sandbox.
"""

from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Mapping

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.daytona_image import apply_image_plan, plan_image

logger = logging.getLogger(__name__)


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
            # Cheap preflight: fail fast on a missing key with an actionable message,
            # rather than letting a generic SDK/auth error surface after the (slow)
            # build-and-create round-trip. Account-state issues (suspended org, depleted
            # credits) still need a live call and surface at `acquire`.
            if not os.environ.get("DAYTONA_API_KEY"):
                raise RuntimeError(
                    "DAYTONA_API_KEY is not set; the Daytona provider needs it to "
                    "authenticate. Put it in loopy.env (control-plane creds) or export it "
                    "before `loopy trigger/run --sandbox daytona`."
                )
            try:
                from daytona import AsyncDaytona
            except ImportError as exc:  # pragma: no cover - exercised via the missing-SDK test path
                raise RuntimeError(
                    "the Daytona SDK is not installed; `pip install loopy-core[daytona]`"
                ) from exc
            self._client = AsyncDaytona()  # reads DAYTONA_API_KEY / DAYTONA_API_URL
        return self._client

    def _create_params(self, spec: SandboxSpec, secrets: Mapping[str, str]):
        from daytona import (
            CreateSandboxFromImageParams,
            CreateSandboxFromSnapshotParams,
            Image,
        )

        build = plan_image(spec.image)
        if build.snapshot is not None:
            return CreateSandboxFromSnapshotParams(snapshot=build.snapshot, env_vars=dict(secrets))
        image = apply_image_plan(build, Image)
        return CreateSandboxFromImageParams(image=image, env_vars=dict(secrets))

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> DaytonaSandbox:
        client = self._ensure_client()
        params = self._create_params(spec, secrets)
        # Lifecycle breadcrumbs: the build + boot is multiple minutes of otherwise-silent
        # wait (apt/npm/substrate compose, then the container start). Logged at INFO so a
        # first-timer can tell "building" from "hung" without changing default stdout.
        if getattr(params, "snapshot", None):
            logger.info("daytona: creating sandbox from snapshot %s", params.snapshot)
        else:
            logger.info(
                "daytona: building image and starting sandbox (this can take a few minutes)…"
            )
        sandbox = await client.create(params)
        logger.info("daytona: sandbox %s started", getattr(sandbox, "id", "?"))
        return DaytonaSandbox(client, sandbox)

    async def aclose(self) -> None:
        """Close the underlying Daytona client (its aiohttp session/connector).

        The one-shot `trigger` path creates a client but nothing ever closed it, so every
        run dumped `Unclosed client session` / `Unclosed connector` warnings to stderr
        *after* a green run — noise that reads like an error and buries real warnings. The
        CLI calls this in a `finally` around the run. Idempotent and injection-safe."""
        if self._client is not None:
            await self._client.close()
            self._client = None
