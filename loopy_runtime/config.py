"""Deployment defaults for `loopy run`, resolved from CLI flags and the environment.

Scope is deliberately tight: the sensor-webhook bind address (`--host`/`--port`), the event-bus
backend (`--bus`), and the state store (`--state`/`--state-path`). Connection strings and provider
keys stay in the environment, never in code — `redis_url` is resolved from the env var `REDIS_URL`
(see `resolve_redis_url`). For local dev those env vars can be supplied from `loopy.env` (the
secret file `loopy_runtime.secrets.load_control_plane_env` reads), merged into the process env
before resolution.

Precedence is applied by the CLI via `resolve()`: explicit flag > auto-detect. For the bus,
auto-detect reads `REDIS_URL` (the same env var `resolve_redis_url` uses, fed by `loopy.env`): a
connection string present ⇒ the networked `redis` bus, absent ⇒ the single-process `inproc` bus.
So writing `REDIS_URL` is enough to opt into Redis, and `--bus inproc` still forces the
single-process bus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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


class ConfigError(Exception):
    """Raised when a CLI flag value is invalid (e.g. an unknown `--bus` or `--state`)."""


@dataclass(frozen=True)
class LoopyConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # `None` means "no bus pinned" — `resolve()` then auto-detects from REDIS_URL. An explicit
    # `--bus inproc`/`redis` is carried as-is and wins over auto-detect.
    bus: str | None = None
    state_backend: str = DEFAULT_STATE
    state_path: str = DEFAULT_STATE_PATH


def _resolve_bus(flag: str | None, *, redis_url: str | None = None) -> str:
    """Pick the event-bus backend: `--bus` flag > auto-detect from REDIS_URL.

    With no flag pinning the bus, auto-detect from the Redis connection string: a `--redis-url`
    flag or a `REDIS_URL` in the environment (which `loopy.env` feeds) selects the networked
    `redis` bus; with none, the single-process `inproc` bus. So writing `REDIS_URL` at `loopy
    init` is enough to opt in with no flag, and `--bus inproc` always forces in-process. The
    returned value is validated by the caller (`resolve`).
    """
    if flag is not None:
        return flag
    return "redis" if (redis_url or os.environ.get("REDIS_URL")) else DEFAULT_BUS


def resolve(
    *,
    host: str | None = None,
    port: int | None = None,
    bus: str | None = None,
    state_backend: str | None = None,
    state_path: str | None = None,
    redis_url: str | None = None,
) -> LoopyConfig:
    """Build the runtime config from explicit CLI flags over built-in defaults (None = not passed).

    The bus follows flag > auto-detect (see `_resolve_bus`); `redis_url` is the `--redis-url` flag,
    consulted only for that auto-detection. Validates the final `bus`/`state_backend`, so an
    invalid `--bus`/`--state` flag is reported as a ConfigError (a clean CLI error) rather than
    surfacing later as a factory ValueError.
    """
    resolved = LoopyConfig(
        host=host if host is not None else DEFAULT_HOST,
        port=port if port is not None else DEFAULT_PORT,
        bus=_resolve_bus(bus, redis_url=redis_url),
        state_backend=state_backend if state_backend is not None else DEFAULT_STATE,
        state_path=state_path if state_path is not None else DEFAULT_STATE_PATH,
    )
    if resolved.bus not in VALID_BUS:
        raise ConfigError(f"'bus' must be one of {VALID_BUS} (got {resolved.bus!r})")
    if resolved.state_backend not in VALID_STATE:
        raise ConfigError(f"'state' must be one of {VALID_STATE} (got {resolved.state_backend!r})")
    return resolved


def resolve_redis_url(flag: str | None) -> str:
    """Resolve the Redis URL: --redis-url flag > REDIS_URL env var > built-in default.

    Connection strings are environment/secret material. The env var may itself be supplied for
    local dev via `loopy.env` (loaded into the process env before this runs); the flag still wins
    over both.
    """
    return flag or os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL
