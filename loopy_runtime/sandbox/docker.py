"""Docker sandbox provider — run agents in a local Docker container behind the
`SandboxProvider`/`Sandbox` Protocols.

This is the hermetic local option: unlike `LocalSubprocessSandbox` (which would run on
the bare host), the agent runs inside a container built from the spec's `image:`, so its
`PATH`/`HOME`/toolchain come from the image and nothing leaks in from the developer's
machine — the same isolation story as the remote Daytona provider, but needing only a
local Docker daemon rather than the full Daytona stack.

Image mapping reuses `plan_image` for validation (unknown keys, secret-in-`env`, bad base
combos) and then derives a concrete Docker base ref + setup commands. The `docker` calls
go through an injectable `run` coroutine so tests never shell out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.sandbox.daytona_image import plan_image

# A keep-alive command that exists on essentially every base image (more portable than
# `sleep infinity`, which busybox lacks): hold the container open so we can `docker exec`
# the agent into it across the step's lifetime, then `docker rm -f` on release.
_KEEPALIVE = ["tail", "-f", "/dev/null"]
_DEFAULT_BASE = "python:3.12-slim"
_DEFAULT_WORKDIR = "/workspace"

Runner = Callable[[list[str]], Awaitable[ExecResult]]


@dataclass
class DockerImagePlan:
    """A loopy `image:` spec resolved to a Docker base ref + run-time setup."""

    base: str
    workdir: str
    env: dict[str, str] = field(default_factory=dict)  # non-secret env baked by image.env
    setup: list[str] = field(default_factory=list)  # shell commands run after container start


def plan_docker_image(image: dict | str | None) -> DockerImagePlan:
    """Map a loopy `image:` spec to a Docker base image + setup commands.

    Validation is shared with the Daytona path via `plan_image` (so unknown keys, secrets in
    `image.env`, and illegal base/snapshot combos fail identically). The build layers
    (`apt`/`pip`/`run`) become shell commands replayed with `docker exec` after the container
    starts — lighter than a full `docker build` and enough for the local "just try it" path.
    """
    plan_image(image)  # reuse validation; raises on bad keys / secret env / bad combos
    if image is None:
        image = {}
    if isinstance(image, str):
        image = {"base": image}

    workdir = str(image.get("workdir") or _DEFAULT_WORKDIR)
    env = {str(k): str(v) for k, v in (image.get("env") or {}).items()}
    return DockerImagePlan(
        base=_base_ref(image), workdir=workdir, env=env, setup=_setup_commands(image)
    )


def _base_ref(image: dict) -> str:
    if "snapshot" in image:
        return str(image["snapshot"])  # a prebuilt image tag to pull
    if "base" in image:
        return str(image["base"])
    if "dockerfile" in image:
        raise ValueError(
            "the docker local provider can't build from a Dockerfile yet; "
            "use base: <image> or debian_slim: <python-version>"
        )
    if "debian_slim" in image:
        # Daytona's debian_slim arg is a *Python* version on Debian slim; the Docker
        # equivalent is the official python:<ver>-slim image.
        version = image["debian_slim"]
        return f"python:{version}-slim" if version else _DEFAULT_BASE
    return _DEFAULT_BASE


def _setup_commands(image: dict) -> list[str]:
    cmds: list[str] = []
    if image.get("apt"):
        pkgs = " ".join(image["apt"])
        cmds.append(f"apt-get update && apt-get install -y {pkgs} && rm -rf /var/lib/apt/lists/*")
    if image.get("pip"):
        cmds.append("pip install " + " ".join(image["pip"]))
    if image.get("pip_requirements"):
        cmds.append(f"pip install -r {image['pip_requirements']}")
    cmds.extend(str(c) for c in (image.get("run") or []))
    return cmds


class DockerSandbox:
    def __init__(self, container_id: str, workdir: str, run: Runner):
        self.id = container_id
        self._workdir = workdir
        self._run = run

    async def exec(self, cmd: list[str]) -> ExecResult:
        # `docker exec` takes a real argv, so the agent command is passed verbatim (no shell
        # re-parsing); `-w` runs it in the image's workdir. stdout/stderr stay separate.
        return await self._run(["docker", "exec", "-w", self._workdir, self.id, *cmd])

    async def release(self) -> None:
        await self._run(["docker", "rm", "-f", self.id])


class DockerSandboxProvider:
    def __init__(self, run: Runner | None = None):
        # `run` shells out to `docker`; injectable so tests never touch a real daemon.
        self._run = run or _subprocess_run

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> DockerSandbox:
        plan = plan_docker_image(spec.image)

        # Start a detached, kept-alive container. Image env first, then secrets/tokens, so
        # injected creds win on conflict (matching the runtime's secrets-merge order). Per-host
        # egress allowlisting (spec.network) isn't enforced locally yet — same gap as Daytona.
        argv = ["docker", "run", "-d", "-w", plan.workdir]
        for key, value in {**plan.env, **secrets}.items():
            argv += ["-e", f"{key}={value}"]
        argv += [plan.base, *_KEEPALIVE]

        started = await self._run(argv)
        if started.exit_code != 0:
            detail = started.stderr.strip() or started.stdout.strip()
            raise RuntimeError(f"docker run failed: {detail}")
        container_id = started.stdout.strip()
        sandbox = DockerSandbox(container_id, plan.workdir, self._run)

        # Replay the build layers (apt/pip/run) inside the running container.
        await self._run(["docker", "exec", container_id, "sh", "-c", f"mkdir -p {plan.workdir}"])
        for command in plan.setup:
            result = await self._run(
                ["docker", "exec", "-w", plan.workdir, container_id, "sh", "-c", command]
            )
            if result.exit_code != 0:
                await sandbox.release()
                raise RuntimeError(f"image setup failed ({command!r}): {result.stderr.strip()}")
        return sandbox


async def _subprocess_run(argv: list[str]) -> ExecResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # docker not installed / not on PATH
        raise RuntimeError(
            "the docker CLI was not found; install Docker or use `--sandbox local`"
        ) from exc
    out, err = await proc.communicate()
    return ExecResult(
        exit_code=proc.returncode or 0,
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace"),
    )
