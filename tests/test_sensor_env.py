"""Sensor-layer secrets: a runner-wide `sensors/.env`, parsed at run time (never compiled).

Sensors run in-process today, so their secrets are a single dotenv merged into the process
env — distinct from the per-sandbox `env_file` references the registry carries.
"""

from __future__ import annotations

from loopy_runtime.secrets import load_sensor_env


def test_load_sensor_env_reads_sensors_dotenv(tmp_path):
    (tmp_path / "sensors").mkdir()
    (tmp_path / "sensors" / ".env").write_text(
        'SENTRY_TOKEN="abc123"\n# a comment\nFOO=bar\n\n'
    )
    assert load_sensor_env(tmp_path) == {"SENTRY_TOKEN": "abc123", "FOO": "bar"}


def test_load_sensor_env_absent_is_empty(tmp_path):
    # Sensor secrets are optional — no file means an empty map, not an error.
    assert load_sensor_env(tmp_path) == {}


def test_load_sensor_env_ignores_other_dotenvs(tmp_path):
    # Only `sensors/.env` is the sensor surface; a sandbox env_file must not leak in.
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "default.env").write_text("DAYTONA_API_KEY=should-not-load\n")
    assert load_sensor_env(tmp_path) == {}
