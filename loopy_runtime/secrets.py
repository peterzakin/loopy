"""Secrets resolution (§6) — load env files into an env map.

Three surfaces, one parser:
  * **Sandbox secrets** — defined at the sandbox (decision), resolved here at run time,
    injected into the sandbox, never logged/recorded. Two sources feed a sandbox's env: the
    `env_file` path(s) it references (the local-dev source, bare canonical keys), and the
    engine's own process environment under a per-sandbox namespace (the production source).
    For each name the sandbox declares in `env:`, the engine reads `<PREFIX>_<KEY>` (e.g.
    `BASESANDBOX_ANTHROPIC_API_KEY`) and injects the canonical `<KEY>`, so two sandboxes can
    hold different values for the same key and the namespaced value wins over the file.
  * **Sensor secrets** — a single runner-wide `sensors/.env` (`load_sensor_env`); sensors run
    in-process and trusted-by-co-location today, so they share the engine's process env rather
    than a per-sensor reference.
  * **Control-plane env** — infra creds the *engine itself* needs (`REDIS_URL`,
    `DAYTONA_API_KEY`/`DAYTONA_API_URL`) in `loopy.env` at the project root
    (`load_control_plane_env`). A local-dev convenience; in production these come from the
    platform's process env. The admin-dashboard bearer token (`LOOPY_ADMIN_TOKEN`, plus
    `LOOPY_ADMIN_TOKEN_NEXT` during rotation) rides the same channel — see
    `docs/design/admin-auth.md`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from loopy_core.registry.sandbox_env import sandbox_env_prefix
from loopy_runtime.manifest_model import SandboxSpec

# The sensor layer's dotenv, relative to the project root. Runner-wide (one file for all
# sensors), gitignored, never compiled into the manifest.
SENSOR_ENV_FILE = "sensors/.env"

# The control-plane dotenv, relative to the project root — infra creds for the engine itself.
# Explicitly named (not a bare `.env`) so its scope is unambiguous: connection strings /
# provider keys only, never agent or sensor secrets.
CONTROL_PLANE_ENV_FILE = "loopy.env"

# Recognized control-plane env keys for the admin dashboard's bearer auth
# (`docs/design/admin-auth.md`). On an operator's laptop these live in `loopy.env`; on a
# hosted control plane they come from the platform's process env (which always wins — the
# caller merges the dotenv with `setdefault`). `*_NEXT` is the rotation overlap slot: the
# server accepts both while the laptop and the platform env roll to the new value.
ADMIN_TOKEN_ENV = "LOOPY_ADMIN_TOKEN"
ADMIN_TOKEN_NEXT_ENV = "LOOPY_ADMIN_TOKEN_NEXT"


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
    """Resolves a sandbox's env from two sources: its `env_file`(s) and namespaced passthrough.

    `env_file` (the local-dev source) is read relative to the project root. Then, for each name
    the sandbox declares in `env:`, the sandbox's `<PREFIX>_<KEY>` variable in `environ` (the
    engine's process env — the production source) is injected under the canonical `<KEY>`,
    overriding any `env_file` value so the real/platform environment always wins. `environ`
    defaults to `os.environ` and is injectable for tests. Values are never logged or recorded.
    """

    def __init__(self, root: str | Path, environ: Mapping[str, str] | None = None):
        self.root = Path(root).resolve()
        self._environ = os.environ if environ is None else environ

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
        # Production passthrough: pull each declared key from the sandbox's namespace in the
        # engine environment, stripping the prefix. A declared key that is absent in both the
        # env_file and the environment is simply not injected (the harness's own required-key
        # check surfaces a genuinely missing credential with an actionable message).
        prefix = sandbox_env_prefix(sandbox_name)
        for key in spec.env:
            namespaced = f"{prefix}_{key}"
            if namespaced in self._environ:
                env[key] = self._environ[namespaced]
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


def write_control_plane_env(root: str | Path, updates: Mapping[str, str]) -> Path:
    """Merge `updates` into the control-plane dotenv (`loopy.env`), in place.

    Used by onboarding (`loopy auth github`) to land creds without a manual edit.
    Existing keys are rewritten in place; new keys are appended; comments,
    blank lines, and unrelated keys are preserved untouched (no clobber).

    A commented stub (e.g. the scaffold's `# GITHUB_APP_ID=` placeholder) for a key
    we're writing is replaced in place by the real `KEY=value`. Without this the stub
    line — being a comment — wouldn't match, so the value would be appended at the
    bottom and the now-misleading `# GITHUB_APP_ID=` stub would linger above it.

    Idempotent — re-running with the same values is a no-op on content. Returns
    the file path. Values are never logged.
    """
    path = Path(root) / CONTROL_PLANE_ENV_FILE
    existing = path.read_text().splitlines() if path.is_file() else []
    remaining = dict(updates)
    lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                lines.append(f"{key}={remaining.pop(key)}")
                continue
        elif stripped.startswith("#"):
            # Commented stub like `# GITHUB_APP_ID=`: if it names a key we're writing,
            # replace it in place rather than leaving the stub and appending below.
            uncommented = stripped.lstrip("#").strip()
            if "=" in uncommented:
                key = uncommented.partition("=")[0].strip()
                if key and key in remaining:
                    lines.append(f"{key}={remaining.pop(key)}")
                    continue
        lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def load_control_plane_env(root: str | Path) -> dict[str, str]:
    """Load the control-plane dotenv (`loopy.env` under the project root) into an env map.

    Holds the infra creds the engine itself needs — `REDIS_URL`,
    `DAYTONA_API_KEY`/`DAYTONA_API_URL` — plus the admin-dashboard bearer token
    (`LOOPY_ADMIN_TOKEN`/`LOOPY_ADMIN_TOKEN_NEXT`). Returns an empty map when the file is
    absent. The caller merges these into the process env
    with `setdefault` (non-override), so a value set in the real/platform environment always
    wins: this file is a local-dev convenience, not the production mechanism. Provider keys and
    connection strings only — keep agent secrets (sandbox `env_file`) and sensor secrets
    (`sensors/.env`) in their own files. Never logged or written to the manifest.
    """
    path = Path(root) / CONTROL_PLANE_ENV_FILE
    if not path.is_file():
        return {}
    return _parse_dotenv(path.read_text())
