"""Secrets resolution (§6) — load env files into an env map.

Two surfaces, one parser:
  * **Sandbox secrets** — defined at the sandbox (decision), referenced by path in the
    manifest, resolved here at run time, injected into the sandbox, never logged/recorded.
  * **Sensor secrets** — a single runner-wide `sensors/.env` (`load_sensor_env`); sensors run
    in-process and trusted-by-co-location today, so they share the engine's process env rather
    than a per-sensor reference. See ARCHITECTURE.md §6.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from loopy_runtime.manifest_model import SandboxSpec

# The sensor layer's dotenv, relative to the project root. Runner-wide (one file for all
# sensors), gitignored, never compiled into the manifest.
SENSOR_ENV_FILE = "sensors/.env"


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


def load_sensor_env(root: str | Path) -> dict[str, str]:
    """Load the sensor layer's dotenv (`sensors/.env` under the project root) into an env map.

    Sensors run in-process and trusted-by-co-location today, so their secrets are a single
    runner-wide dotenv rather than the per-sandbox `env_file` references the registry carries.
    Returns an empty map when the file is absent (sensor secrets are optional). The caller
    merges these into the process environment so `@sensor` functions read them via
    `os.environ`. Because sensors share the engine's process env, keep infra creds
    (`DAYTONA_API_KEY`, `REDIS_URL`) out of this file. Never logged or written to the manifest.
    """
    path = Path(root) / SENSOR_ENV_FILE
    if not path.is_file():
        return {}
    return _parse_dotenv(path.read_text())
