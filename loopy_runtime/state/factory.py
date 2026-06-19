"""Select a StateStore by name (lazy imports so the on-disk store's deps load only when used).

Mirrors `make_event_bus` / `make_sandbox_provider`: the in-memory store is the ephemeral,
single-process default for tests and one-shot `trigger`; the SQLite store is the durable
single-file mode `loopy run` uses so run history survives restarts and the `loopy admin`
dashboard (a separate process) can read it.
"""

from __future__ import annotations

from pathlib import Path

# Canonical set of StateStore backend names — the single source of truth for both the factory's
# dispatch below and the config/CLI validation in `loopy_runtime.config`.
VALID_STATE = ("inproc", "sqlite")


def make_state_store(backend: str = "inproc", path: str | Path = "", *, root: Path = Path(".")):
    """Construct the StateStore for `backend`. For `sqlite`, `path` is resolved relative to
    `root` when not absolute, and its parent directory is created (e.g. `.loopy/`)."""
    if backend == "inproc":
        from loopy_runtime.state.inmemory import InMemoryStateStore

        return InMemoryStateStore()
    if backend == "sqlite":
        from loopy_runtime.state.sqlite import SqliteStateStore

        p = Path(path)
        if not p.is_absolute():
            p = root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return SqliteStateStore(p)
    raise ValueError(f"unknown state store {backend!r}; choose one of {VALID_STATE}")
