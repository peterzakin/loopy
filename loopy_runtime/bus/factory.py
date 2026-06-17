"""Select an EventBus by name (lazy imports so the optional redis dep stays optional).

Mirrors `make_sandbox_provider`: the in-process bus is single-machine; the Redis bus is the
networked/decoupled mode (one or more `Runtime` workers consuming a shared stream)."""

from __future__ import annotations

from loopy_runtime.contract import StateStore

_DEFAULT_REDIS_URL = "redis://localhost:6379"


def make_event_bus(
    name: str = "inproc",
    *,
    redis_url: str | None = None,
    state: StateStore | None = None,
):
    if name == "inproc":
        from loopy_runtime.bus.inproc import InProcessEventBus

        return InProcessEventBus()
    if name == "redis":
        from loopy_runtime.bus.redis import RedisEventBus

        return RedisEventBus(redis_url or _DEFAULT_REDIS_URL, state=state)
    raise ValueError(f"unknown event bus {name!r}; choose 'inproc' or 'redis'")
