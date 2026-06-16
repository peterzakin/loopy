"""Local subprocess sandbox (v1) — runs the agent in a temp workdir on the host with
the resolved secrets as env. Isolation is only as strong as the host (dev/demo);
Daytona/containers drop in later behind the same `SandboxProvider` Protocol.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from loopy_runtime.contract import ExecResult
from loopy_runtime.manifest_model import SandboxSpec


class LocalSubprocessSandbox:
    def __init__(self, id: str, workdir: Path, env: Mapping[str, str]):
        self.id = id
        self.workdir = workdir
        self._env = dict(env)

    async def exec(self, cmd: list[str]) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.workdir),
            env=self._env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    async def release(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


class LocalSandboxProvider:
    def __init__(self) -> None:
        self._seq = 0

    async def acquire(
        self, spec: SandboxSpec, secrets: Mapping[str, str]
    ) -> LocalSubprocessSandbox:
        self._seq += 1
        workdir = Path(tempfile.mkdtemp(prefix=f"loopy-sandbox-{self._seq}-"))
        return LocalSubprocessSandbox(id=f"local-{self._seq}", workdir=workdir, env=secrets)
