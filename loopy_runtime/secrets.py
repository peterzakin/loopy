"""Secrets resolution (§6) — load a sandbox's env_file(s) into an env map.

Secrets are defined at the **sandbox** (decision), referenced by path in the manifest,
resolved here at run time, injected into the sandbox, and never logged/recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from loopy_runtime.manifest_model import SandboxSpec


def _parse_dotenv(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class StaticSecretsResolver:
    """Returns a fixed env map regardless of sandbox — for tests/dev (no file I/O)."""

    def __init__(self, env: Mapping[str, str] | None = None):
        self._env = dict(env or {})

    def resolve(self, sandbox_name: str, spec: SandboxSpec) -> dict[str, str]:
        return dict(self._env)


class EnvFileSecretsResolver:
    """Resolves a sandbox's env_file(s) relative to the project root."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, sandbox_name: str, spec: SandboxSpec) -> dict[str, str]:
        env: dict[str, str] = {}
        for rel in spec.env_file:
            path = (self.root / rel).resolve()
            if self.root != path and self.root not in path.parents:
                raise ValueError(
                    f"env_file {rel!r} for sandbox '{sandbox_name}' escapes the project root"
                )
            if not path.is_file():
                raise FileNotFoundError(
                    f"env_file {rel!r} for sandbox '{sandbox_name}' not found at {path}"
                )
            env.update(_parse_dotenv(path.read_text()))
        return env
