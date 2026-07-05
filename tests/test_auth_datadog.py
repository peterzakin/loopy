"""`loopy auth datadog` — unit coverage with no live network.

Stubs `datadog_app._request_json` (the module's single network boundary) and drives the
plain-function core `auth.run_datadog_auth`, so nothing here touches Datadog. Covers the happy
path (create -> mint + persist secret + name, push it into the webhook headers), the URL
resolution and `/hooks/datadog` append, site selection, the idempotency guard, `--update`,
`--manual`, and the 403 scope-guidance exit.
"""

from __future__ import annotations

import json

import pytest
import typer

from loopy_cli import auth
from loopy_runtime.scm import datadog_app
from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env


@pytest.fixture
def fake_api(monkeypatch):
    """Record calls and return canned responses in place of the HTTP boundary."""
    calls = []

    def fake_request(method, url, *, api_key, app_key, payload=None):
        calls.append(
            {
                "method": method,
                "url": url,
                "api_key": api_key,
                "app_key": app_key,
                "payload": payload,
            }
        )
        if method == "POST":
            return {"name": payload["name"], "url": payload["url"]}
        if method == "PUT":
            return {"name": url.rstrip("/").rsplit("/", 1)[-1], "url": payload["url"]}
        if method == "GET":
            return {"name": url.rstrip("/").rsplit("/", 1)[-1]}
        return {}

    monkeypatch.setattr(datadog_app, "_request_json", fake_request)
    return calls


def _clear_env(monkeypatch):
    for var in ("DD_API_KEY", "DD_APP_KEY", "DD_APPLICATION_KEY", "DD_SITE", "LOOPY_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)


def _with_keys(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "api-k")
    monkeypatch.setenv("DD_APP_KEY", "app-k")


# ── happy path ──────────────────────────────────────────────────────────────


def test_create_mints_and_persists_secret_and_name(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://loopy.example.com")

    auth.run_datadog_auth(root=tmp_path)

    stored = load_control_plane_env(tmp_path)
    secret = stored["DATADOG_WEBHOOK_SECRET"]
    assert secret  # minted locally, non-empty
    assert stored["DATADOG_WEBHOOK_NAME"] == "loopy"

    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["api_key"] == "api-k" and post["app_key"] == "app-k"
    assert post["url"].startswith("https://api.datadoghq.com/api/v1/integration/webhooks/")
    assert post["payload"]["url"] == "https://loopy.example.com/hooks/datadog"
    assert post["payload"]["encode_as"] == "json"
    # The minted secret is pushed into the webhook's custom headers (not returned by Datadog).
    headers = json.loads(post["payload"]["custom_headers"])
    assert headers["Authorization"] == f"Bearer {secret}"
    # The canonical payload template travels as a JSON string carrying $-variables.
    assert "$ALERT_TRANSITION" in post["payload"]["payload"]


def test_full_webhook_url_is_left_untouched(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)

    auth.run_datadog_auth(webhook_url="https://h.example.com/hooks/datadog", root=tmp_path)

    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["payload"]["url"] == "https://h.example.com/hooks/datadog"


def test_site_selects_the_api_base(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)

    auth.run_datadog_auth(site="datadoghq.eu", webhook_url="https://h/hooks/datadog", root=tmp_path)
    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["url"].startswith("https://api.datadoghq.eu/api/v1/")


def test_app_key_falls_back_to_dd_application_key(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_API_KEY", "api-k")
    monkeypatch.setenv("DD_APPLICATION_KEY", "app-k2")  # the longer env name

    auth.run_datadog_auth(webhook_url="https://h/hooks/datadog", root=tmp_path)
    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["app_key"] == "app-k2"


# ── guards & branches ───────────────────────────────────────────────────────


def test_existing_secret_without_force_errors(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)
    write_control_plane_env(tmp_path, {"DATADOG_WEBHOOK_SECRET": "old"})

    with pytest.raises(typer.Exit):
        auth.run_datadog_auth(webhook_url="https://h/hooks/datadog", root=tmp_path)
    assert load_control_plane_env(tmp_path)["DATADOG_WEBHOOK_SECRET"] == "old"


def test_force_overwrites(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)
    write_control_plane_env(tmp_path, {"DATADOG_WEBHOOK_SECRET": "old"})

    auth.run_datadog_auth(webhook_url="https://h/hooks/datadog", root=tmp_path, force=True)
    assert load_control_plane_env(tmp_path)["DATADOG_WEBHOOK_SECRET"] != "old"


def test_update_repoints_url_and_preserves_secret(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)
    write_control_plane_env(
        tmp_path, {"DATADOG_WEBHOOK_SECRET": "keep", "DATADOG_WEBHOOK_NAME": "loopy"}
    )

    auth.run_datadog_auth(
        webhook_url="https://new.example.com/hooks/datadog", root=tmp_path, update=True
    )

    put = next(c for c in fake_api if c["method"] == "PUT")
    assert put["url"].endswith("/webhooks/loopy")
    assert put["payload"]["url"] == "https://new.example.com/hooks/datadog"
    # The stored secret is re-sent in the headers so the PUT can't drop it, and stays put locally.
    assert json.loads(put["payload"]["custom_headers"])["Authorization"] == "Bearer keep"
    assert load_control_plane_env(tmp_path)["DATADOG_WEBHOOK_SECRET"] == "keep"


def test_update_without_stored_secret_errors(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)
    with pytest.raises(typer.Exit):
        auth.run_datadog_auth(webhook_url="https://h/hooks/datadog", root=tmp_path, update=True)


def test_manual_writes_secret_without_network(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("manual must not hit the API")

    monkeypatch.setattr(datadog_app, "_request_json", boom)

    auth.run_datadog_auth(root=tmp_path, manual=True)
    stored = load_control_plane_env(tmp_path)
    assert stored["DATADOG_WEBHOOK_SECRET"]
    assert stored["DATADOG_WEBHOOK_NAME"] == "loopy"
    out = capsys.readouterr().out
    # Prints the payload template and the header carrying the minted secret to paste by hand.
    assert "$ALERT_TRANSITION" in out
    assert f"Bearer {stored['DATADOG_WEBHOOK_SECRET']}" in out


def test_403_gives_scope_guidance(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)

    def forbidden(method, url, *, api_key, app_key, payload=None):
        raise datadog_app.DatadogAPIError(403, "forbidden")

    monkeypatch.setattr(datadog_app, "_request_json", forbidden)

    with pytest.raises(typer.Exit):
        auth.run_datadog_auth(webhook_url="https://h/hooks/datadog", root=tmp_path)
    err = capsys.readouterr().err
    assert "manage integrations" in err and "Application" in err


def test_localhost_url_warns_but_proceeds(tmp_path, monkeypatch, fake_api, capsys):
    _clear_env(monkeypatch)
    _with_keys(monkeypatch)

    auth.run_datadog_auth(webhook_url="http://127.0.0.1:8000/hooks/datadog", root=tmp_path)
    assert "won't reach it" in capsys.readouterr().out
    assert load_control_plane_env(tmp_path)["DATADOG_WEBHOOK_SECRET"]


# ── `loopy auth datadog`: status when registered, else create ────────────────


def _invoke(args):
    from typer.testing import CliRunner

    from loopy_cli import app

    return CliRunner().invoke(app, args)


def test_command_shows_status_when_registered(tmp_path, monkeypatch):
    write_control_plane_env(
        tmp_path, {"DATADOG_WEBHOOK_SECRET": "sec", "DATADOG_WEBHOOK_NAME": "loopy"}
    )
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_datadog_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "datadog", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "loopy" in result.output and "configured" in result.output
    assert ran["auth"] is False


def test_command_starts_auth_when_unregistered(tmp_path, monkeypatch):
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_datadog_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "datadog", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert ran["auth"] is True


def test_command_force_reruns_auth_even_when_registered(tmp_path, monkeypatch):
    write_control_plane_env(tmp_path, {"DATADOG_WEBHOOK_SECRET": "sec"})
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_datadog_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "datadog", "--root", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert ran["auth"] is True
