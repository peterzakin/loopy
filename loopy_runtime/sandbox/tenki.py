"""Tenki sandbox provider: run agents in Tenki cloud sandboxes behind the
`SandboxProvider`/`Sandbox` Protocols.

Mirrors the Daytona provider: the `tenki-sandbox` SDK is imported lazily (so the rest of
the runtime loads without it) and its async client can be injected, so tests never hit the
real service. Secrets inject as the sandbox's env at create, `exec` maps an argv list to
a `CommandResult` and returns an `ExecResult`, and `release` terminates the sandbox.

Image handling: create the sandbox from Tenki's DEFAULT image (its registry
can't resolve public docker refs), then replay the composed image's `apt`/`pip`/`run` layers
as `exec` setup commands after it starts — reusing the docker provider's validated plan
(`plan_docker_image`) so the two can't drift. Two quirks of Tenki's default image the replay
accounts for: it runs as a NON-ROOT user (with passwordless sudo), so system-package steps
(`apt`) are sudo-prefixed while user-space steps (`npm`/`pip`) are not; and commands run in
the sandbox's own writable home rather than a forced `/workspace`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.docker import plan_docker_image

logger = logging.getLogger(__name__)

# Orphan-VM backstop (seconds): if the engine dies without calling release(), the sandbox
# self-terminates after this long. It bounds a leaked VM — loopy's per-step budget is the real
# run limit — but it is a HARD cap, so it must exceed your longest expected run. Override with
# TENKI_MAX_DURATION_S when a step can run longer than the default hour.
_DEFAULT_MAX_DURATION_S = 3600  # 1 hour


def _max_duration_s() -> int:
    raw = os.environ.get("TENKI_MAX_DURATION_S")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        logger.warning(
            "tenki: ignoring invalid TENKI_MAX_DURATION_S=%r; using %ds",
            raw,
            _DEFAULT_MAX_DURATION_S,
        )
    return _DEFAULT_MAX_DURATION_S


def _needs_root(command: str) -> bool:
    """A replayed setup command that installs system packages needs root. On Tenki's non-root
    default image those run via its passwordless sudo; user-space steps (`npm -g`, `pip`) must
    NOT be sudo'd — `npm -g` under root uses a different toolchain and misbehaves."""
    return any(tok in command for tok in ("apt-get", "apt ", "dpkg", "apt-key"))


def _stderr_with_diagnostics(result) -> str:
    """`CommandResult` carries `reason`/`signal`/`errno` alongside `stderr`. We collect all that
    and show two sections: the stderr (skipped if blank) and the diganostics, with the extra
    details."""
    stderr = result.stderr_text or ""
    details = [
        f"{name}={value}"
        for name, value in (
            ("reason", getattr(result, "reason", None)),
            ("signal", getattr(result, "signal", None)),
            ("errno", getattr(result, "errno", None)),
        )
        if value
    ]
    if not details:
        return stderr
    diagnostics = "tenki exec: " + ", ".join(details)
    return f"{stderr}\n{diagnostics}" if stderr.strip() else diagnostics


class TenkiSandbox:
    def __init__(self, client, sandbox, workdir: str | None = None):
        self._client = client
        self._sandbox = sandbox
        self._workdir = workdir  # None → the sandbox's own (writable) home
        self.id = getattr(sandbox, "id", "tenki")

    async def exec(self, cmd: list[str]) -> ExecResult:
        # Tenki's exec takes a real argv (no shell re-parsing). Env was baked at create, so
        # it isn't re-passed here (avoids replacing the container's PATH); `cwd=None` runs in
        # the sandbox home.
        result = await self._sandbox.exec(*cmd, cwd=self._workdir)
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout_text,
            stderr=_stderr_with_diagnostics(result),
        )

    async def release(self) -> None:
        await self._sandbox.terminate()


class TenkiSandboxProvider:
    def __init__(self, client=None):
        # `client` is an AsyncClient instance; injectable for tests.
        self._client = client
        # Resolved once and cached: Tenki's create requires a project and its workspace,
        # which aren't env vars — they come from the account (`who_am_i`) or explicit override.
        self._project_id: str | None = None
        self._workspace_id: str | None = None

    def _ensure_client(self):
        if self._client is None:
            # Cheap preflight: fail fast on a missing key rather than after a slow create
            # round-trip. Account-state issues still surface live at `acquire`.
            if not (os.environ.get("TENKI_API_KEY") or os.environ.get("TENKI_AUTH_TOKEN")):
                raise RuntimeError(
                    "TENKI_API_KEY is not set; the Tenki provider needs it to authenticate. "
                    "Put it in loopy.env or export it before running a sandbox with "
                    "`provider: tenki`."
                )
            try:
                from tenki_sandbox import AsyncClient
            except ImportError as exc:
                raise RuntimeError(
                    "the tenki-sandbox SDK is not installed; it's an optional dependency of "
                    "loopy-computer. Install it with `pip install loopy-computer[tenki]`."
                ) from exc
            self._client = AsyncClient()  # reads TENKI_API_KEY / TENKI_AUTH_TOKEN from env
        return self._client

    async def _resolve_target(self, client) -> tuple[str, str | None]:
        """The (project_id, workspace_id) Tenki's `create` needs, resolved once and cached.

        Precedence: explicit `TENKI_PROJECT_ID` / `TENKI_WORKSPACE_ID` (from loopy.env) win;
        otherwise the account's first workspace + first project (`who_am_i`), which is the
        zero-config path for a single-project account. A multi-project account is logged so
        the user knows to pin it with the env overrides."""
        if self._project_id is not None:
            return self._project_id, self._workspace_id
        env_project = os.environ.get("TENKI_PROJECT_ID")
        if env_project:
            self._project_id = env_project
            self._workspace_id = os.environ.get("TENKI_WORKSPACE_ID")
            return self._project_id, self._workspace_id

        identity = await client.who_am_i()
        workspaces = list(getattr(identity, "workspaces", None) or [])
        if not workspaces:
            raise RuntimeError(
                "tenki: your account has no workspace; set TENKI_PROJECT_ID in loopy.env"
            )
        workspace = workspaces[0]
        projects = list(getattr(workspace, "projects", None) or [])
        if not projects:
            raise RuntimeError(
                f"tenki: workspace {getattr(workspace, 'id', '?')!r} has no project; "
                "set TENKI_PROJECT_ID in loopy.env"
            )
        if len(workspaces) > 1 or len(projects) > 1:
            logger.info(
                "tenki: account has multiple workspaces/projects; using workspace %s / "
                "project %s (pin with TENKI_WORKSPACE_ID / TENKI_PROJECT_ID in loopy.env)",
                getattr(workspace, "id", "?"),
                getattr(projects[0], "id", "?"),
            )
        self._workspace_id = getattr(workspace, "id", None)
        self._project_id = getattr(projects[0], "id", None)
        return self._project_id, self._workspace_id

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> TenkiSandbox:
        client = self._ensure_client()
        project_id, workspace_id = await self._resolve_target(client)
        # Reuse the docker provider's validated base+setup plan (shared `plan_image`
        # validation underneath): a base image ref, a workdir, non-secret image env, and
        # the apt/pip/run layers to replay after start.
        plan = plan_docker_image(spec.image)

        # Tenki resolves `image=` against the workspace registry, so a public docker ref
        # (what plan.base is) fails with "registry image not found". Here, we create
        # from tenki's DEFAULT image and replay the build layers below.
        logger.info(
            "tenki: creating sandbox (default image; loopy base %r not yet mapped, project %s)…",
            plan.base,
            project_id,
        )
        # Image env first, then injected secrets, so creds win on conflict (matches the
        # runtime's secrets-merge order). `create` waits for the sandbox to be ready.
        sandbox = await client.create(
            env={**plan.env, **dict(secrets)},
            project_id=project_id,
            workspace_id=workspace_id,
            max_duration=_max_duration_s(),
        )
        box = TenkiSandbox(client, sandbox)  # cwd = the sandbox's own writable home
        logger.info("tenki: sandbox %s started; provisioning toolchain…", box.id)

        # Provisioning must be failure-atomic: once create() returns, ANY failure below — a
        # setup command exiting non-zero, an exec that raises, or cancellation (CancelledError
        # is a BaseException) — has to terminate the VM, or it leaks (billed until max_duration).
        # Replay the composed build layers: `apt` steps are sudo'd (non-root default image),
        # user-space steps (`npm`/`pip`) run as the user, all in the sandbox home (no /workspace).
        try:
            for command in plan.setup:
                argv = ["sudo", "sh", "-c", command] if _needs_root(command) else ["sh", "-c", command]
                # Go through box.exec so a setup failure carries the same reason/signal/errno
                # diagnostics as any other exec (its .stderr is already enriched).
                result = await box.exec(argv)
                if result.exit_code != 0:
                    raise RuntimeError(
                        f"tenki: image setup failed ({command!r}): {result.stderr.strip()}"
                    )
        except BaseException:
            # Shield so the terminate completes even if we're being cancelled; swallow any
            # cleanup error so the original failure is what propagates.
            try:
                await asyncio.shield(box.release())
            except BaseException:
                logger.warning("tenki: cleanup after provisioning failure also failed", exc_info=True)
            raise
        logger.info("tenki: sandbox %s ready", box.id)
        return box

    async def aclose(self) -> None:
        """Close the underlying Tenki client, mirroring the Daytona provider's cleanup so the
        one-shot `trigger` path doesn't leak an open client. Idempotent and injection-safe."""
        if self._client is not None:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                await close()
            self._client = None
