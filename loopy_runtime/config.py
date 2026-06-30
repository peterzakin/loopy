"""loopy.yaml — deployment defaults for `loopy run`, mapping ~1:1 to CLI flags.

Scope is deliberately tight (see plans/future/config): the sensor-webhook bind address
(`sensor_server.host/port`) and the event-bus backend (`bus`). Connection strings and provider
keys stay in the environment, never YAML — `redis_url` is resolved from the env var `REDIS_URL`
(see `resolve_redis_url`). For local dev those env vars can be supplied from `loopy.env` (the
secret companion to this file; `loopy_runtime.secrets.load_control_plane_env`), merged into the
process env before resolution. `sandbox` is registry-owned and `root` is per-invocation, so
neither lives here.

Precedence is applied by the CLI via `resolve()`: explicit flag > loopy.yaml > auto-detect. For
the bus, auto-detect reads `REDIS_URL` (the same env var `resolve_redis_url` uses, fed by
`loopy.env`): a connection string present ⇒ the networked `redis` bus, absent ⇒ the single-process
`inproc` bus. So writing `REDIS_URL` is enough to opt into Redis, and `--bus inproc` still forces
the single-process bus. Parses with ruamel.yaml to match the compile frontend
(`loopy_core/registry/loader.py`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from loopy_runtime.bus.factory import VALID_BUS  # single source of truth for bus names
from loopy_runtime.state.factory import VALID_STATE  # single source of truth for state names

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_BUS = "inproc"
DEFAULT_REDIS_URL = "redis://localhost:6379"
# `loopy run` defaults to a durable on-disk store so run history survives restarts and the
# `loopy admin` dashboard can read it without any flag; `.loopy/` is already gitignored. The
# in-memory store stays the opt-out (`--state inproc`) and the default for one-shot `trigger`.
DEFAULT_STATE = "sqlite"
DEFAULT_STATE_PATH = ".loopy/state.db"

# Reserved-but-not-built keys (TODO #1 limits). Recognized so a forward-looking config doesn't
# trip the unknown-key warning, but intentionally not parsed in v1.
_RESERVED_TOP_LEVEL = ("limits",)
_KNOWN_TOP_LEVEL = ("sensor_server", "bus", "state", *_RESERVED_TOP_LEVEL)
_KNOWN_SENSOR_SERVER = ("host", "port")
_KNOWN_STATE = ("backend", "path")

_yaml = YAML(typ="safe")


class ConfigError(Exception):
    """Raised when loopy.yaml exists but is unparseable or malformed."""


@dataclass(frozen=True)
class LoopyConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # `None` means "no bus pinned" — `resolve()` then auto-detects from REDIS_URL. An explicit
    # `inproc`/`redis` (from loopy.yaml or `--bus`) is carried as-is and wins over auto-detect.
    bus: str | None = None
    state_backend: str = DEFAULT_STATE
    state_path: str = DEFAULT_STATE_PATH


def _default_warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def load_config(path: Path, *, on_warning: Callable[[str], None] = _default_warn) -> LoopyConfig:
    """Load `loopy.yaml` into a LoopyConfig. Missing file → all defaults (backward compatible).

    Unknown keys warn (not fatal); a present-but-unparseable or wrong-shaped file raises
    ConfigError so the CLI can report it and exit non-zero.
    """
    if not path.exists():
        return LoopyConfig()

    try:
        data = _yaml.load(path.read_text())
    except (YAMLError, OSError) as exc:
        raise ConfigError(f"{path} is unparseable: {exc}") from exc

    if data is None:
        return LoopyConfig()  # empty file
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path} must be a mapping (got {type(data).__name__})")

    for key in data:
        if key not in _KNOWN_TOP_LEVEL:
            on_warning(f"{path}: unknown key '{key}' ignored")

    host, port = DEFAULT_HOST, DEFAULT_PORT
    server = data.get("sensor_server")
    if server is not None:
        if not isinstance(server, Mapping):
            raise ConfigError(f"{path}: 'sensor_server' must be a mapping")
        for key in server:
            if key not in _KNOWN_SENSOR_SERVER:
                on_warning(f"{path}: unknown key 'sensor_server.{key}' ignored")
        if "host" in server:
            host = str(server["host"])
        if "port" in server:
            port = server["port"]
            if not isinstance(port, int) or isinstance(port, bool):
                raise ConfigError(f"{path}: 'sensor_server.port' must be an integer")

    # Unspecified (`None`) is preserved so `resolve()` can auto-detect from REDIS_URL; only a
    # present-but-invalid value is an error.
    bus = data.get("bus")
    if bus is not None and (not isinstance(bus, str) or bus not in VALID_BUS):
        raise ConfigError(f"{path}: 'bus' must be one of {VALID_BUS} (got {bus!r})")

    state_backend, state_path = DEFAULT_STATE, DEFAULT_STATE_PATH
    state_cfg = data.get("state")
    if state_cfg is not None:
        if not isinstance(state_cfg, Mapping):
            raise ConfigError(f"{path}: 'state' must be a mapping")
        for key in state_cfg:
            if key not in _KNOWN_STATE:
                on_warning(f"{path}: unknown key 'state.{key}' ignored")
        if "backend" in state_cfg:
            state_backend = str(state_cfg["backend"])
            if state_backend not in VALID_STATE:
                raise ConfigError(
                    f"{path}: 'state.backend' must be one of {VALID_STATE} (got {state_backend!r})"
                )
        if "path" in state_cfg:
            state_path = str(state_cfg["path"])

    return LoopyConfig(
        host=host, port=port, bus=bus, state_backend=state_backend, state_path=state_path
    )


def _resolve_bus(flag: str | None, configured: str | None, *, redis_url: str | None = None) -> str:
    """Pick the event-bus backend: `--bus` flag > loopy.yaml `bus:` > auto-detect from REDIS_URL.

    With neither a flag nor a file pinning the bus, auto-detect from the Redis connection string:
    a `--redis-url` flag or a `REDIS_URL` in the environment (which `loopy.env` feeds) selects the
    networked `redis` bus; with none, the single-process `inproc` bus. So writing `REDIS_URL` at
    `loopy init` is enough to opt in with no flag, and `--bus inproc` always forces in-process.
    The returned value is validated by the caller (`resolve`).
    """
    if flag is not None:
        return flag
    if configured is not None:
        return configured
    return "redis" if (redis_url or os.environ.get("REDIS_URL")) else DEFAULT_BUS


def resolve(
    config: LoopyConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    bus: str | None = None,
    state_backend: str | None = None,
    state_path: str | None = None,
    redis_url: str | None = None,
) -> LoopyConfig:
    """Overlay explicit CLI flags onto a loaded config (flag wins; None means 'not passed').

    The bus follows flag > file > auto-detect (see `_resolve_bus`); `redis_url` is the `--redis-url`
    flag, consulted only for that auto-detection. Validates the final `bus`/`state_backend`, so an
    invalid `--bus`/`--state` flag is reported as a ConfigError (the same clean path as an invalid
    file value) rather than surfacing later as a factory ValueError.
    """
    resolved = LoopyConfig(
        host=host if host is not None else config.host,
        port=port if port is not None else config.port,
        bus=_resolve_bus(bus, config.bus, redis_url=redis_url),
        state_backend=state_backend if state_backend is not None else config.state_backend,
        state_path=state_path if state_path is not None else config.state_path,
    )
    if resolved.bus not in VALID_BUS:
        raise ConfigError(f"'bus' must be one of {VALID_BUS} (got {resolved.bus!r})")
    if resolved.state_backend not in VALID_STATE:
        raise ConfigError(f"'state' must be one of {VALID_STATE} (got {resolved.state_backend!r})")
    return resolved


def resolve_redis_url(flag: str | None) -> str:
    """Resolve the Redis URL: --redis-url flag > REDIS_URL env var > built-in default.

    Connection strings are environment/secret material, so this is never read from loopy.yaml.
    The env var may itself be supplied for local dev via `loopy.env` (loaded into the process
    env before this runs); the flag still wins over both.
    """
    return flag or os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL
