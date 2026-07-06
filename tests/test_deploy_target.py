"""`loopy_cli.deploy_target` — bootstrap client-side hints and the CloudFront signal."""

from __future__ import annotations

import pytest

from loopy_cli.deploy_target import (
    BOOTSTRAP_ENGINE_PORT_ENV,
    BOOTSTRAP_INSTANCE_ID_ENV,
    is_cloudfront_url,
    resolve_bootstrap_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        BOOTSTRAP_INSTANCE_ID_ENV,
        BOOTSTRAP_ENGINE_PORT_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


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


def test_is_cloudfront_url():
    assert is_cloudfront_url("https://d1234abcd.cloudfront.net")
    assert is_cloudfront_url("https://d1234abcd.cloudfront.net/admin")
    assert is_cloudfront_url("https://ABC.CLOUDFRONT.NET")  # case-insensitive host
    # not CloudFront: a custom domain, a lookalike suffix, or nothing at all
    assert not is_cloudfront_url("https://loopy.example.com")
    assert not is_cloudfront_url("https://cloudfront.net.evil.com")
    assert not is_cloudfront_url("")
    assert not is_cloudfront_url(None)


def test_render_target_constants():
    from loopy_cli.deploy_target import RENDER_SERVICE_ID_ENV, TARGET_RENDER

    assert TARGET_RENDER == "render"
    assert RENDER_SERVICE_ID_ENV == "LOOPY_RENDER_SERVICE_ID"
