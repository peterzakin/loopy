"""CLI flag / env resolution for `loopy run` (loopy_runtime/config.py)."""

from __future__ import annotations

import pytest

from loopy_runtime.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_REDIS_URL,
    DEFAULT_STATE,
    DEFAULT_STATE_PATH,
    ConfigError,
    LoopyConfig,
    hook_url,
    resolve,
    resolve_public_url,
    resolve_redis_url,
)


def test_no_flags_is_all_defaults(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve() == LoopyConfig(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        bus="inproc",
        state_backend=DEFAULT_STATE,
        state_path=DEFAULT_STATE_PATH,
    )


def test_state_defaults_to_durable_sqlite(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    cfg = resolve()
    assert (cfg.state_backend, cfg.state_path) == (DEFAULT_STATE, DEFAULT_STATE_PATH)
    assert DEFAULT_STATE == "sqlite"  # `loopy run` is durable by default


def test_resolve_flags_override_defaults(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    out = resolve(host="flag-host", port=9001, bus="redis")
    assert (out.host, out.port, out.bus) == ("flag-host", 9001, "redis")


def test_resolve_bad_bus_flag_raises():
    # An invalid --bus flag must surface as a ConfigError (clean CLI error), not a factory
    # ValueError leaking from make_event_bus later.
    with pytest.raises(ConfigError):
        resolve(bus="kafka")


def test_resolve_state_flags_override():
    out = resolve(state_backend="inproc", state_path="x.db")
    assert (out.state_backend, out.state_path) == ("inproc", "x.db")


def test_resolve_bad_state_flag_raises():
    with pytest.raises(ConfigError):
        resolve(state_backend="postgres")


def test_resolve_bus_auto_redis_from_env(monkeypatch):
    """Unspecified bus + REDIS_URL in the environment ⇒ the networked redis bus, no flag needed."""
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve().bus == "redis"


def test_resolve_bus_auto_inproc_without_redis(monkeypatch):
    """Unspecified bus + no REDIS_URL ⇒ the single-process in-memory bus."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve().bus == "inproc"


def test_resolve_bus_redis_url_flag_triggers_redis(monkeypatch):
    """The --redis-url flag selects the redis bus on its own (no env var, no --bus)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve(redis_url="redis://flag:6379").bus == "redis"


def test_resolve_bus_flag_overrides_redis_env(monkeypatch):
    """`--bus inproc` forces in-process even when REDIS_URL would otherwise auto-select redis."""
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve(bus="inproc").bus == "inproc"


def test_redis_url_flag_wins(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve_redis_url("redis://flag:6379") == "redis://flag:6379"


def test_redis_url_env_used(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve_redis_url(None) == "redis://env:6379"


def test_redis_url_default(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve_redis_url(None) == DEFAULT_REDIS_URL


# ── public URL (shared across providers) ────────────────────────────────────


def test_public_url_flag_wins(monkeypatch):
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://env.example.com")
    assert resolve_public_url("https://flag.example.com") == "https://flag.example.com"


def test_public_url_from_env(monkeypatch):
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://env.example.com")
    assert resolve_public_url(None) == "https://env.example.com"


def test_public_url_none_when_unset(monkeypatch):
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)
    assert resolve_public_url(None) is None


def test_public_url_reads_passed_env_mapping(monkeypatch):
    """Callers with a merged loopy.env view pass it explicitly (process env is not consulted)."""
    monkeypatch.delenv("LOOPY_PUBLIC_URL", raising=False)
    assert resolve_public_url(None, env={"LOOPY_PUBLIC_URL": "https://merged"}) == "https://merged"


def test_hook_url_appends_path_to_bare_base():
    assert (
        hook_url("https://x.example.com", "/hooks/sentry") == "https://x.example.com/hooks/sentry"
    )
    assert (
        hook_url("https://x.example.com/", "/hooks/github") == "https://x.example.com/hooks/github"
    )


def test_hook_url_leaves_a_full_url_untouched():
    full = "https://x.example.com/hooks/sentry"
    assert hook_url(full, "/hooks/sentry") == full
