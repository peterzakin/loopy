"""Env-file surfaces parsed at run time (never compiled): the sensor-layer `sensors/.env`
and the control-plane `loopy.env`. Both merge into the process env — distinct from the
per-sandbox `env_file` references the registry carries.
"""

from __future__ import annotations

from loopy_runtime.secrets import load_control_plane_env, load_sensor_env


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


def test_load_control_plane_env_reads_loopy_env(tmp_path):
    (tmp_path / "loopy.env").write_text(
        "REDIS_URL=redis://h:6379/0\n# infra creds\nDAYTONA_API_KEY=dt-abc\n"
    )
    assert load_control_plane_env(tmp_path) == {
        "REDIS_URL": "redis://h:6379/0",
        "DAYTONA_API_KEY": "dt-abc",
    }


def test_load_control_plane_env_absent_is_empty(tmp_path):
    assert load_control_plane_env(tmp_path) == {}


def test_control_plane_and_sensor_envs_are_separate_files(tmp_path):
    # The two process-env surfaces are distinct files: each reads only its own.
    (tmp_path / "loopy.env").write_text("REDIS_URL=redis://h:6379\n")
    (tmp_path / "sensors").mkdir()
    (tmp_path / "sensors" / ".env").write_text("SENTRY_TOKEN=tok\n")
    assert load_control_plane_env(tmp_path) == {"REDIS_URL": "redis://h:6379"}
    assert load_sensor_env(tmp_path) == {"SENTRY_TOKEN": "tok"}
