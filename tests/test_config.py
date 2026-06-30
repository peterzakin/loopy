"""loopy.yaml config loading + flag/env resolution (loopy_runtime/config.py)."""

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
    load_config,
    resolve,
    resolve_redis_url,
)


def _write(tmp_path, text: str):
    p = tmp_path / "loopy.yaml"
    p.write_text(text)
    return p


def test_missing_file_is_all_defaults(tmp_path):
    cfg = load_config(tmp_path / "loopy.yaml")
    # bus is left unspecified (None) at load time — resolve() auto-detects it from REDIS_URL.
    assert cfg == LoopyConfig(host=DEFAULT_HOST, port=DEFAULT_PORT, bus=None)


def test_empty_file_is_defaults(tmp_path):
    assert load_config(_write(tmp_path, "")) == LoopyConfig()


def test_yaml_values_loaded(tmp_path):
    cfg = load_config(
        _write(tmp_path, "sensor_server:\n  host: 0.0.0.0\n  port: 9001\nbus: redis\n")
    )
    assert (cfg.host, cfg.port, cfg.bus) == ("0.0.0.0", 9001, "redis")


def test_partial_yaml_keeps_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "sensor_server:\n  port: 9999\n"))
    # No `bus:` key ⇒ unspecified (None); resolve() decides the effective bus.
    assert (cfg.host, cfg.port, cfg.bus) == (DEFAULT_HOST, 9999, None)


def test_unknown_keys_warn_not_fatal(tmp_path):
    warnings: list[str] = []
    path = _write(tmp_path, "bus: inproc\nnope: 1\nsensor_server:\n  host: h\n  bogus: 2\n")
    cfg = load_config(path, on_warning=warnings.append)
    assert cfg.host == "h"
    assert any("nope" in w for w in warnings)
    assert any("sensor_server.bogus" in w for w in warnings)


def test_reserved_keys_do_not_warn(tmp_path):
    warnings: list[str] = []
    load_config(_write(tmp_path, "limits:\n  max_spend: 5\n"), on_warning=warnings.append)
    assert warnings == []


def test_state_defaults_to_durable_sqlite(tmp_path):
    cfg = load_config(tmp_path / "loopy.yaml")
    assert (cfg.state_backend, cfg.state_path) == (DEFAULT_STATE, DEFAULT_STATE_PATH)
    assert DEFAULT_STATE == "sqlite"  # `loopy run` is durable by default


def test_state_block_loaded(tmp_path):
    cfg = load_config(_write(tmp_path, "state:\n  backend: inproc\n  path: runs.db\n"))
    assert (cfg.state_backend, cfg.state_path) == ("inproc", "runs.db")


def test_bad_state_backend_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "state:\n  backend: postgres\n"))


def test_unknown_state_key_warns(tmp_path):
    warnings: list[str] = []
    load_config(_write(tmp_path, "state:\n  bogus: 1\n"), on_warning=warnings.append)
    assert any("state.bogus" in w for w in warnings)


def test_non_mapping_state_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "state: memory\n"))


def test_malformed_yaml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "sensor_server: [unclosed\n"))


def test_non_mapping_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "- just\n- a\n- list\n"))


def test_bad_bus_value_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "bus: kafka\n"))


def test_non_integer_port_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "sensor_server:\n  port: eighty\n"))


def test_resolve_flag_overrides_config():
    base = LoopyConfig(host="cfg-host", port=1, bus="inproc")
    assert resolve(base, host="flag-host", bus="redis") == LoopyConfig("flag-host", 1, "redis")


def test_resolve_none_keeps_config():
    base = LoopyConfig(host="cfg-host", port=1, bus="redis")
    assert resolve(base) == base


def test_resolve_bad_bus_flag_raises():
    # An invalid --bus flag must surface as a ConfigError (clean CLI error), not a factory
    # ValueError leaking from make_event_bus later.
    with pytest.raises(ConfigError):
        resolve(LoopyConfig(), bus="kafka")


def test_resolve_state_flags_override():
    base = LoopyConfig(state_backend="sqlite", state_path=".loopy/state.db")
    out = resolve(base, state_backend="inproc", state_path="x.db")
    assert (out.state_backend, out.state_path) == ("inproc", "x.db")


def test_resolve_bad_state_flag_raises():
    with pytest.raises(ConfigError):
        resolve(LoopyConfig(), state_backend="postgres")


def test_resolve_bus_auto_redis_from_env(monkeypatch):
    """Unspecified bus + REDIS_URL in the environment ⇒ the networked redis bus, no flag needed."""
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve(LoopyConfig()).bus == "redis"


def test_resolve_bus_auto_inproc_without_redis(monkeypatch):
    """Unspecified bus + no REDIS_URL ⇒ the single-process in-memory bus."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve(LoopyConfig()).bus == "inproc"


def test_resolve_bus_redis_url_flag_triggers_redis(monkeypatch):
    """The --redis-url flag selects the redis bus on its own (no env var, no --bus)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve(LoopyConfig(), redis_url="redis://flag:6379").bus == "redis"


def test_resolve_bus_flag_overrides_redis_env(monkeypatch):
    """`--bus inproc` forces in-process even when REDIS_URL would otherwise auto-select redis."""
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve(LoopyConfig(), bus="inproc").bus == "inproc"


def test_resolve_config_bus_wins_over_env(monkeypatch):
    """An explicit loopy.yaml `bus:` beats REDIS_URL auto-detection (file is intentional)."""
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve(LoopyConfig(bus="inproc")).bus == "inproc"


def test_redis_url_flag_wins(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve_redis_url("redis://flag:6379") == "redis://flag:6379"


def test_redis_url_env_used(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://env:6379")
    assert resolve_redis_url(None) == "redis://env:6379"


def test_redis_url_default(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert resolve_redis_url(None) == DEFAULT_REDIS_URL
