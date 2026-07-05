"""`loopy auth sentry` — unit coverage with no live network.

Stubs `sentry_app._request_json` (the module's single network boundary) and drives the
plain-function core `auth.run_sentry_auth`, so nothing here touches Sentry. Covers the
happy path (create -> persist secret + slug), org auto-detect, the URL resolution and
`/hooks/sentry` append, the idempotency guard, `--update`, `--manual`, and the 403
scope-guidance exit.
"""

from __future__ import annotations

import pytest
import typer

from loopy_cli import auth
from loopy_runtime.scm import sentry_app
from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env


@pytest.fixture
def fake_api(monkeypatch):
    """Record calls and return canned responses in place of the HTTP boundary."""
    calls = []

    def fake_request(method, url, *, token, payload=None):
        calls.append({"method": method, "url": url, "token": token, "payload": payload})
        if method == "GET" and url.endswith("/organizations/"):
            return [{"slug": "acme"}]
        if method == "POST" and url.endswith("/sentry-apps/"):
            return {"slug": "loopy-a1b2", "clientSecret": "sec_live_xyz"}
        if method == "PUT":
            return {"slug": url.rstrip("/").rsplit("/", 1)[-1], "webhookUrl": payload["webhookUrl"]}
        if method == "GET":  # get_integration
            return {"slug": "loopy-a1b2"}
        return {}

    monkeypatch.setattr(sentry_app, "_request_json", fake_request)
    return calls


def _clear_env(monkeypatch):
    for var in ("SENTRY_AUTH_TOKEN", "SENTRY_ORG", "SENTRY_URL", "LOOPY_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)


# ── happy path ──────────────────────────────────────────────────────────────


def test_create_persists_secret_and_slug(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://loopy.example.com")

    auth.run_sentry_auth(org="acme", root=tmp_path)

    stored = load_control_plane_env(tmp_path)
    assert stored["SENTRY_WEBHOOK_SECRET"] == "sec_live_xyz"
    assert stored["SENTRY_APP_SLUG"] == "loopy-a1b2"

    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["token"] == "tok"
    assert post["payload"]["isInternal"] is True
    assert post["payload"]["events"] == ["issue"]
    # LOOPY_PUBLIC_URL got /hooks/sentry appended.
    assert post["payload"]["webhookUrl"] == "https://loopy.example.com/hooks/sentry"


def test_org_is_auto_detected_when_single(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://loopy.example.com")

    auth.run_sentry_auth(root=tmp_path)  # no --org, no $SENTRY_ORG

    post = next(c for c in fake_api if c["method"] == "POST")
    assert "/organizations/acme/sentry-apps/" in post["url"]


def test_full_webhook_url_is_left_untouched(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")

    auth.run_sentry_auth(
        org="acme", webhook_url="https://h.example.com/hooks/sentry", root=tmp_path
    )

    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["payload"]["webhookUrl"] == "https://h.example.com/hooks/sentry"


def test_self_hosted_base_url_is_used(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")

    auth.run_sentry_auth(
        org="acme",
        webhook_url="https://h/hooks/sentry",
        sentry_url="https://sentry.internal",
        root=tmp_path,
    )
    post = next(c for c in fake_api if c["method"] == "POST")
    assert post["url"].startswith("https://sentry.internal/api/0/")


# ── guards & branches ───────────────────────────────────────────────────────


def test_existing_secret_without_force_errors(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    write_control_plane_env(tmp_path, {"SENTRY_WEBHOOK_SECRET": "old"})

    with pytest.raises(typer.Exit):
        auth.run_sentry_auth(org="acme", webhook_url="https://h/hooks/sentry", root=tmp_path)
    # untouched
    assert load_control_plane_env(tmp_path)["SENTRY_WEBHOOK_SECRET"] == "old"


def test_force_overwrites(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    write_control_plane_env(tmp_path, {"SENTRY_WEBHOOK_SECRET": "old"})

    auth.run_sentry_auth(
        org="acme", webhook_url="https://h/hooks/sentry", root=tmp_path, force=True
    )
    assert load_control_plane_env(tmp_path)["SENTRY_WEBHOOK_SECRET"] == "sec_live_xyz"


def test_update_repoints_without_touching_secret(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    write_control_plane_env(
        tmp_path, {"SENTRY_WEBHOOK_SECRET": "keep", "SENTRY_APP_SLUG": "loopy-a1b2"}
    )

    auth.run_sentry_auth(
        webhook_url="https://new.example.com/hooks/sentry", root=tmp_path, update=True
    )

    put = next(c for c in fake_api if c["method"] == "PUT")
    assert put["url"].endswith("/sentry-apps/loopy-a1b2/")
    assert put["payload"]["webhookUrl"] == "https://new.example.com/hooks/sentry"
    assert load_control_plane_env(tmp_path)["SENTRY_WEBHOOK_SECRET"] == "keep"  # untouched


def test_update_without_stored_slug_errors(tmp_path, monkeypatch, fake_api):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    with pytest.raises(typer.Exit):
        auth.run_sentry_auth(webhook_url="https://h/hooks/sentry", root=tmp_path, update=True)


def test_manual_stores_pasted_secret_without_network(tmp_path, monkeypatch):
    _clear_env(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("manual must not hit the API")

    monkeypatch.setattr(sentry_app, "_request_json", boom)
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "pasted_secret")

    auth.run_sentry_auth(root=tmp_path, manual=True)
    stored = load_control_plane_env(tmp_path)
    assert stored["SENTRY_WEBHOOK_SECRET"] == "pasted_secret"
    assert "SENTRY_APP_SLUG" not in stored


def test_403_gives_scope_guidance(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "ci-token")
    monkeypatch.setenv("SENTRY_ORG", "acme")

    def forbidden(method, url, *, token, payload=None):
        raise sentry_app.SentryAPIError(403, "forbidden")

    monkeypatch.setattr(sentry_app, "_request_json", forbidden)

    with pytest.raises(typer.Exit):
        auth.run_sentry_auth(webhook_url="https://h/hooks/sentry", root=tmp_path)
    err = capsys.readouterr().err
    assert "org:write" in err and "Organization Auth Token" in err


def test_localhost_url_warns_but_proceeds(tmp_path, monkeypatch, fake_api, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")

    auth.run_sentry_auth(
        org="acme", webhook_url="http://127.0.0.1:8000/hooks/sentry", root=tmp_path
    )
    assert "won't reach it" in capsys.readouterr().out
    assert load_control_plane_env(tmp_path)["SENTRY_WEBHOOK_SECRET"] == "sec_live_xyz"


# ── `loopy auth sentry`: status when registered, else create ─────────────────


def _invoke(args):
    from typer.testing import CliRunner

    from loopy_cli import app

    return CliRunner().invoke(app, args)


def test_command_shows_status_when_registered(tmp_path, monkeypatch):
    """A stored secret makes `loopy auth sentry` print status, not re-run token collection."""
    write_control_plane_env(
        tmp_path, {"SENTRY_WEBHOOK_SECRET": "sec", "SENTRY_APP_SLUG": "loopy-a1b2"}
    )
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_sentry_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "sentry", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "loopy-a1b2" in result.output and "configured" in result.output
    assert ran["auth"] is False  # showed status, did not start token collection


def test_command_starts_auth_when_unregistered(tmp_path, monkeypatch):
    """With no stored secret, `loopy auth sentry` starts token collection."""
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_sentry_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "sentry", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert ran["auth"] is True


def test_command_force_reruns_auth_even_when_registered(tmp_path, monkeypatch):
    """--force overrides the status short-circuit and re-runs token collection."""
    write_control_plane_env(tmp_path, {"SENTRY_WEBHOOK_SECRET": "sec"})
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_sentry_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "sentry", "--root", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert ran["auth"] is True
