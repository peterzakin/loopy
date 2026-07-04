"""`loopy_cli.deploy_target` — resolving the recorded deploy target (and its legacy spelling)."""

from __future__ import annotations

import pytest

from loopy_cli.deploy_target import (
    BOOTSTRAP_ENGINE_PORT_ENV,
    BOOTSTRAP_INSTANCE_ID_ENV,
    DEPLOY_TARGET_ENV,
    TARGET_BOOTSTRAP,
    TARGET_BYO,
    resolve_bootstrap_config,
    resolve_deploy_target,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        DEPLOY_TARGET_ENV,
        BOOTSTRAP_INSTANCE_ID_ENV,
        BOOTSTRAP_ENGINE_PORT_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


def test_unset_resolves_to_none(tmp_path):
    assert resolve_deploy_target(tmp_path) is None


def test_reads_loopy_env(tmp_path):
    (tmp_path / "loopy.env").write_text(f"{DEPLOY_TARGET_ENV}=bootstrap\n")
    assert resolve_deploy_target(tmp_path) == TARGET_BOOTSTRAP


def test_process_env_wins_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / "loopy.env").write_text(f"{DEPLOY_TARGET_ENV}=bootstrap\n")
    monkeypatch.setenv(DEPLOY_TARGET_ENV, "byo")
    assert resolve_deploy_target(tmp_path) == TARGET_BYO


def test_stray_value_resolves_to_none(tmp_path):
    # `local` isn't a hosting choice, and junk must not be trusted either.
    for stray in ("local", "render", "yes"):
        (tmp_path / "loopy.env").write_text(f"{DEPLOY_TARGET_ENV}={stray}\n")
        assert resolve_deploy_target(tmp_path) is None


def test_bootstrap_config_reads_recorded_hints(tmp_path, monkeypatch):
    # absent everywhere: simply missing
    assert resolve_bootstrap_config(tmp_path) == {}

    (tmp_path / "loopy.env").write_text(
        f"{BOOTSTRAP_INSTANCE_ID_ENV}=i-0abc\n{BOOTSTRAP_ENGINE_PORT_ENV}=8443\n"
    )
    config = resolve_bootstrap_config(tmp_path)
    assert config[BOOTSTRAP_INSTANCE_ID_ENV] == "i-0abc"
    assert config[BOOTSTRAP_ENGINE_PORT_ENV] == "8443"
    # process env wins over the dotenv
    monkeypatch.setenv(BOOTSTRAP_INSTANCE_ID_ENV, "i-0def")
    assert resolve_bootstrap_config(tmp_path)[BOOTSTRAP_INSTANCE_ID_ENV] == "i-0def"
