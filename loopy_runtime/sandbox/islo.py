"""Islo sandbox provider — run agents in hosted Islo Linux sandboxes.

The Islo SDK is imported lazily so Loopy projects that do not use `provider: islo`
do not need to import it. A client can be injected for tests, keeping unit tests offline.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.providers import RECOGNIZED_MODEL_KEYS

_DEFAULT_IMAGE = "docker.io/library/ubuntu:26.04"
_DEFAULT_WORKDIR = "/workspace/loopy"
_DEFAULT_USER = "root"
_TERMINAL_FAILURE = {"failed", "error", "cancelled", "canceled"}


@dataclass(frozen=True)
class IsloImagePlan:
    image: str = _DEFAULT_IMAGE
    workdir: str = _DEFAULT_WORKDIR
    user: str = _DEFAULT_USER
    env: dict[str, str] = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None


def _looks_like_secret(key: str) -> bool:
    return key in RECOGNIZED_MODEL_KEYS or key.endswith(("_API_KEY", "_TOKEN", "_SECRET"))


def plan_islo_image(image: dict | str | None) -> IsloImagePlan:
    """Map Loopy's image spec to Islo create params plus post-create setup commands."""

    if image is None:
        image = {}
    if isinstance(image, str):
        image = {"image": image}
    if not isinstance(image, dict):
        raise ValueError(f"image: must be a mapping or string, got {type(image).__name__}")

    known = {
        "image",
        "base",
        "debian_slim",
        "workdir",
        "env",
        "apt",
        "pip",
        "pip_requirements",
        "run",
        "user",
        "vcpus",
        "memory_mb",
        "disk_gb",
    }
    unknown = set(image) - known
    if unknown:
        raise ValueError(f"unknown Islo image keys: {sorted(unknown)}")

    base_keys = [key for key in ("image", "base", "debian_slim") if key in image]
    if len(base_keys) > 1:
        raise ValueError(f"image: declares multiple Islo image selectors {base_keys}; choose one")

    image_ref = _DEFAULT_IMAGE
    if "image" in image:
        image_ref = str(image["image"])
    elif "base" in image:
        image_ref = str(image["base"])
    elif "debian_slim" in image:
        version = str(image["debian_slim"] or "3.12")
        image_ref = f"docker.io/library/python:{version}-slim"

    workdir = str(image.get("workdir") or _DEFAULT_WORKDIR)
    user = str(image.get("user") or _DEFAULT_USER)

    env = {str(key): str(value) for key, value in dict(image.get("env") or {}).items()}
    leaked = sorted(key for key in env if _looks_like_secret(key))
    if leaked:
        raise ValueError(
            f"image.env must not contain secrets {leaked}; secrets are injected at run "
            "time via the sandbox env_file, not baked into the image"
        )

    setup: list[str] = [f"mkdir -p {shlex.quote(workdir)}"]
    if image.get("apt"):
        pkgs = " ".join(shlex.quote(str(pkg)) for pkg in image["apt"])
        setup.append(f"apt-get update && apt-get install -y {pkgs} && rm -rf /var/lib/apt/lists/*")
    if image.get("pip"):
        pkgs = " ".join(shlex.quote(str(pkg)) for pkg in image["pip"])
        setup.append(f"python -m pip install {pkgs}")
    if image.get("pip_requirements"):
        setup.append(f"python -m pip install -r {shlex.quote(str(image['pip_requirements']))}")
    setup.extend(str(command) for command in image.get("run") or [])

    return IsloImagePlan(
        image=image_ref,
        workdir=workdir,
        user=user,
        env=env,
        setup=setup,
        vcpus=_positive_int(image.get("vcpus"), "vcpus"),
        memory_mb=_positive_int(image.get("memory_mb"), "memory_mb"),
        disk_gb=_positive_int(image.get("disk_gb"), "disk_gb"),
    )


def _positive_int(value, key: str) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"image.{key} must be positive")
    return parsed


class IsloSandbox:
    def __init__(self, client, sandbox, *, workdir: str, user: str, poll_interval: float = 1.0):
        self._client = client
        self._sandbox = sandbox
        self._workdir = workdir
        self._user = user
        self._poll_interval = poll_interval
        self.id = getattr(sandbox, "name", None) or getattr(sandbox, "id", "islo")

    async def exec(self, cmd: list[str]) -> ExecResult:
        return await self._run(cmd, workdir=self._workdir, user=self._user)

    async def release(self) -> None:
        await self._client.sandboxes.delete_sandbox(sandbox_name=self.id)

    async def setup(self, commands: list[str]) -> None:
        for command in commands:
            result = await self._run(
                ["bash", "-lc", command], workdir=self._workdir, user=self._user
            )
            if result.exit_code != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Islo setup failed ({command!r}): {detail}")

    async def _run(self, cmd: list[str], *, workdir: str, user: str) -> ExecResult:
        started = await self._client.sandboxes.exec_in_sandbox(
            sandbox_name=self.id,
            command=list(cmd),
            workdir=workdir,
            user=user,
        )
        exec_id = started.exec_id
        while True:
            result = await self._client.sandboxes.get_exec_result(
                sandbox_name=self.id,
                exec_id=exec_id,
            )
            exit_code = getattr(result, "exit_code", None)
            status = str(getattr(result, "status", "")).lower()
            if exit_code is not None:
                return ExecResult(
                    exit_code=int(exit_code),
                    stdout=str(getattr(result, "stdout", "") or ""),
                    stderr=str(getattr(result, "stderr", "") or ""),
                )
            if status in _TERMINAL_FAILURE:
                return ExecResult(
                    exit_code=1,
                    stdout=str(getattr(result, "stdout", "") or ""),
                    stderr=str(getattr(result, "stderr", "") or status),
                )
            await asyncio.sleep(self._poll_interval)


class IsloSandboxProvider:
    def __init__(self, client=None, *, poll_interval: float = 1.0):
        self._client = client
        self._poll_interval = poll_interval

    def _ensure_client(self):
        if self._client is None:
            if not os.environ.get("ISLO_API_KEY"):
                raise RuntimeError(
                    "ISLO_API_KEY is not set; the Islo provider needs it to authenticate. "
                    "Put it in loopy.env (control-plane creds) or export it before running "
                    "a sandbox with `provider: islo`."
                )
            try:
                from islo import AsyncIslo
            except ImportError as exc:  # pragma: no cover - only a broken install hits
                raise RuntimeError(
                    "the Islo SDK failed to import; install it with `pip install islo` "
                    "or reinstall loopy with the Islo dependency available"
                ) from exc
            self._client = AsyncIslo()
        return self._client

    async def acquire(self, spec: SandboxSpec, secrets: Mapping[str, str]) -> IsloSandbox:
        client = self._ensure_client()
        plan = plan_islo_image(spec.image)
        env = {**plan.env, **dict(secrets)}
        name = f"loopy-{uuid4().hex[:12]}"
        sandbox = await client.sandboxes.create_sandbox(
            name=name,
            image=plan.image,
            workdir=plan.workdir,
            env=env,
            vcpus=plan.vcpus,
            memory_mb=plan.memory_mb,
            disk_gb=plan.disk_gb,
        )
        wrapped = IsloSandbox(
            client, sandbox, workdir=plan.workdir, user=plan.user, poll_interval=self._poll_interval
        )
        try:
            await wrapped.setup(plan.setup)
        except Exception:
            await wrapped.release()
            raise
        return wrapped

    async def aclose(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self._client = None
