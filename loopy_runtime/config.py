"""loopy.yaml — deployment defaults for `loopy run`, mapping ~1:1 to CLI flags.

Scope is deliberately tight (see plans/future/config): the sensor-webhook bind address
(`sensor_server.host/port`) and the event-bus backend (`bus`). Connection strings and provider
keys stay in the environment, never YAML — `redis_url` is resolved from the env var `REDIS_URL`
(see `resolve_redis_url`). For local dev those env vars can be supplied from `loopy.env` (the
secret companion to this file; `loopy_runtime.secrets.load_control_plane_env`), merged into the
process env before resolution. `sandbox` is registry-owned and `root` is per-invocation, so
neither lives here.

Precedence is applied by the CLI via `resolve()`: explicit flag > loopy.yaml > built-in default.
Parses with ruamel.yaml to match the compile frontend (`loopy_core/registry/loader.py`).
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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_BUS = "inproc"
DEFAULT_REDIS_URL = "redis://localhost:6379"

# Reserved-but-not-built keys (TODO #1 limits, TODO #2 state). Recognized so a forward-looking
# config doesn't trip the unknown-key warning, but intentionally not parsed in v1.
_RESERVED_TOP_LEVEL = ("state", "limits")
_KNOWN_TOP_LEVEL = ("sensor_server", "bus", *_RESERVED_TOP_LEVEL)
_KNOWN_SENSOR_SERVER = ("host", "port")

_yaml = YAML(typ="safe")


class ConfigError(Exception):
    """Raised when loopy.yaml exists but is unparseable or malformed."""


@dataclass(frozen=True)
class LoopyConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    bus: str = DEFAULT_BUS


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

    bus = data.get("bus", DEFAULT_BUS)
    if not isinstance(bus, str) or bus not in VALID_BUS:
        raise ConfigError(f"{path}: 'bus' must be one of {VALID_BUS} (got {bus!r})")

    return LoopyConfig(host=host, port=port, bus=bus)


def resolve(
    config: LoopyConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    bus: str | None = None,
) -> LoopyConfig:
    """Overlay explicit CLI flags onto a loaded config (flag wins; None means 'not passed').

    Validates the final `bus`, so an invalid `--bus` flag is reported as a ConfigError (the same
    clean path as an invalid file value) rather than surfacing later as a factory ValueError.
    """
    resolved = LoopyConfig(
        host=host if host is not None else config.host,
        port=port if port is not None else config.port,
        bus=bus if bus is not None else config.bus,
    )
    if resolved.bus not in VALID_BUS:
        raise ConfigError(f"'bus' must be one of {VALID_BUS} (got {resolved.bus!r})")
    return resolved


def resolve_redis_url(flag: str | None) -> str:
    """Resolve the Redis URL: --redis-url flag > REDIS_URL env var > built-in default.

    Connection strings are environment/secret material, so this is never read from loopy.yaml.
    The env var may itself be supplied for local dev via `loopy.env` (loaded into the process
    env before this runs); the flag still wins over both.
    """
    return flag or os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL
