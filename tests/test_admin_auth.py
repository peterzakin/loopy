"""Admin dashboard auth (`docs/design/admin-auth.md`): edge auth on /api/*, the fail-closed
serve entry, and the `loopy admin --remote` BFF proxy.

Server-side tests go through a real request cycle (TestClient) because the point is the
dependency that runs *before* the handlers — awaiting endpoints directly would skip it.
Proxy tests swap the network for an `httpx.MockTransport`.
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from loopy_runtime.dashboard.app import create_app, mount_admin
from loopy_runtime.dashboard.auth import (
    TOKEN_PREFIX,
    AdminAuth,
    generate_admin_token,
    is_loopback_host,
)
from loopy_runtime.dashboard.proxy import create_proxy_app, validate_remote_url
from loopy_runtime.state.inmemory import InMemoryStateStore

TOKEN = "loopy_sk_current-token"
NEXT = "loopy_sk_next-token"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep the admin env keys out of (and cleaned from) the real process env — the CLI
    command setdefaults values from loopy.env into os.environ, which monkeypatch alone
    would not undo."""
    keys = (
        "LOOPY_ADMIN_TOKEN",
        "LOOPY_ADMIN_TOKEN_NEXT",
        "LOOPY_PUBLIC_URL",
        "PORT",
        "LOOPY_DEPLOY_TARGET",
        "LOOPY_BOOTSTRAP_INSTANCE_ID",
        "LOOPY_BOOTSTRAP_ENGINE_PORT",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in keys:
        os.environ.pop(key, None)


# ── AdminAuth ────────────────────────────────────────────────────────────────────────
def test_from_env_returns_none_without_tokens():
    assert AdminAuth.from_env({}) is None
    assert AdminAuth.from_env({"LOOPY_ADMIN_TOKEN": "  "}) is None


def test_from_env_reads_current_and_next():
    auth = AdminAuth.from_env({"LOOPY_ADMIN_TOKEN": TOKEN, "LOOPY_ADMIN_TOKEN_NEXT": NEXT})
    assert auth.check(f"Bearer {TOKEN}") and auth.check(f"Bearer {NEXT}")


def test_check_rejects_missing_wrong_scheme_and_wrong_token():
    auth = AdminAuth([TOKEN])
    assert not auth.check(None)
    assert not auth.check("")
    assert not auth.check(TOKEN)  # bare token, no Bearer scheme
    assert not auth.check(f"Basic {TOKEN}")
    assert not auth.check("Bearer nope")


def test_empty_token_list_is_an_error():
    with pytest.raises(ValueError):
        AdminAuth(["", "  "])


def test_repr_never_contains_token_values():
    auth = AdminAuth([TOKEN, NEXT])
    assert TOKEN not in repr(auth) and NEXT not in repr(auth)


def test_generate_admin_token_prefix_and_entropy():
    a, b = generate_admin_token(), generate_admin_token()
    assert a.startswith(TOKEN_PREFIX) and b.startswith(TOKEN_PREFIX)
    assert a != b
    assert len(a) - len(TOKEN_PREFIX) >= 43  # 32 random bytes, urlsafe-b64


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.5")
    assert not is_loopback_host("cp.example.com")


# ── edge auth on the app ─────────────────────────────────────────────────────────────
def _client(auth: AdminAuth | None) -> TestClient:
    return TestClient(create_app(InMemoryStateStore(), auth=auth))


def test_api_401_when_token_missing_or_invalid():
    client = _client(AdminAuth([TOKEN]))
    for path in ("/api/runs", "/api/meta", "/api/workflows", "/api/registry", "/api/sensors"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.headers["www-authenticate"] == "Bearer"
    resp = client.get("/api/runs", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert TOKEN not in resp.text  # the token value never appears in a response


def test_api_200_with_valid_token_and_rotation_overlap():
    client = _client(AdminAuth([TOKEN, NEXT]))
    assert client.get("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/runs", headers={"Authorization": f"Bearer {NEXT}"}).status_code == 200


def test_healthz_and_ui_shell_stay_open():
    client = _client(AdminAuth([TOKEN]))
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 200  # app code, not data


def test_api_open_without_auth_config():
    # local loopback mode — back-compat: no auth object, no bearer required
    assert _client(None).get("/api/runs").status_code == 200


# ── /admin mounted on the engine's webhook server ───────────────────────────────────
def _engine_with_admin(host: str, env: dict) -> tuple[TestClient, str | None]:
    from fastapi import FastAPI

    parent = FastAPI()  # stands in for the sensor runner's webhook app
    mounted = mount_admin(parent, InMemoryStateStore(), host=host, env=env)
    return TestClient(parent), mounted


def test_engine_mounts_admin_open_on_loopback():
    client, mounted = _engine_with_admin("127.0.0.1", {})
    assert mounted == "open (loopback)"
    assert client.get("/admin/api/runs").status_code == 200
    assert client.get("/admin/healthz").json() == {"ok": True}


def test_engine_skips_admin_mount_without_token_on_non_loopback():
    # fail-closed by absence: the engine still serves webhooks, but /admin does not exist
    client, mounted = _engine_with_admin("0.0.0.0", {})
    assert mounted is None
    assert client.get("/admin/api/runs").status_code == 404


def test_engine_mounts_admin_behind_bearer_on_non_loopback():
    client, mounted = _engine_with_admin("0.0.0.0", {"LOOPY_ADMIN_TOKEN": TOKEN})
    assert mounted == "bearer auth"
    assert client.get("/admin/api/runs").status_code == 401
    resp = client.get("/admin/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert client.get("/admin/healthz").status_code == 200  # probes stay open


# ── the fail-closed serve entry (`loopy admin`) ──────────────────────────────────────
def _invoke(args, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from loopy_cli import app

    monkeypatch.chdir(tmp_path)  # no loopy.env / state DB unless a test writes one
    return CliRunner().invoke(app, args)


def test_admin_refuses_non_loopback_bind_without_token(monkeypatch, tmp_path):
    result = _invoke(["admin", "local", "--host", "0.0.0.0"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "LOOPY_ADMIN_TOKEN" in result.output and "refusing" in result.output


def test_admin_non_loopback_proceeds_with_token(monkeypatch, tmp_path):
    # With a token configured the fail-closed gate opens; the next failure is the missing DB,
    # which proves the auth check ran (it comes first) and passed.
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    result = _invoke(["admin", "local", "--host", "0.0.0.0"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "no state DB" in result.output and "LOOPY_ADMIN_TOKEN" not in result.output


def test_admin_reads_token_from_loopy_env(monkeypatch, tmp_path):
    (tmp_path / "loopy.env").write_text(f"LOOPY_ADMIN_TOKEN={TOKEN}\n")
    result = _invoke(["admin", "local", "--host", "0.0.0.0"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "no state DB" in result.output  # past the gate — the dotenv supplied the token


# ── `loopy admin <target>` resolution ────────────────────────────────────────────────
def test_bare_admin_defaults_to_local(monkeypatch, tmp_path):
    # `local` is the default: bare `loopy admin` pairs with a local `loopy run`. With no
    # DB yet, the next failure is the missing state DB — proving it resolved to local.
    result = _invoke(["admin"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "no state DB" in result.output


def test_bare_admin_hints_recorded_target_when_local_db_missing(monkeypatch, tmp_path):
    # A project set up for a hosted target, run with bare `loopy admin`: it still defaults
    # to local, but the missing-DB error points at the target the project actually uses.
    (tmp_path / "loopy.env").write_text("LOOPY_DEPLOY_TARGET=bootstrap\n")
    result = _invoke(["admin"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "no state DB" in result.output
    assert "'bootstrap'" in result.output and "loopy admin bootstrap" in result.output


def test_unknown_target_rejected_with_db_hint(monkeypatch, tmp_path):
    # The positional used to be the state-DB path; a path-looking value gets pointed at --db.
    result = _invoke(["admin", "some.db"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "unknown deploy target" in result.output and "--db" in result.output


def test_unknown_target_rejected_plain(monkeypatch, tmp_path):
    result = _invoke(["admin", "render"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "unknown deploy target 'render'" in result.output
    assert "--db" not in result.output


def test_local_rejects_url_flag(monkeypatch, tmp_path):
    result = _invoke(["admin", "local", "--url", "https://cp.example.com"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "--url" in result.output and "local" in result.output


def test_remote_target_rejects_db_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    result = _invoke(["admin", "byo", "--db", "some.db"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "--db" in result.output and "local" in result.output


def test_admin_port_resolution(monkeypatch):
    from loopy_cli import _admin_port

    monkeypatch.delenv("PORT", raising=False)
    assert _admin_port(1234) == 1234
    assert _admin_port(None) == 9000
    monkeypatch.setenv("PORT", "8080")
    assert _admin_port(None) == 8080  # platform-injected $PORT
    assert _admin_port(1234) == 1234  # explicit flag still wins
    monkeypatch.setenv("PORT", "not-a-port")
    assert _admin_port(None) == 9000


# ── `loopy admin byo` / `loopy admin bootstrap` proxy guards ────────────────────────
def test_byo_refuses_without_token(monkeypatch, tmp_path):
    result = _invoke(["admin", "byo", "--url", "https://cp.example.com"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "LOOPY_ADMIN_TOKEN" in result.output


def test_byo_derives_url_from_loopy_public_url(monkeypatch):
    from loopy_cli import _admin_byo_url

    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://loopy.example.com/")
    assert _admin_byo_url() == "https://loopy.example.com/admin"


def test_byo_requires_loopy_public_url(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    result = _invoke(["admin", "byo"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "LOOPY_PUBLIC_URL" in result.output


def test_byo_derivation_feeds_the_tls_guard(monkeypatch, tmp_path):
    # A plain-HTTP LOOPY_PUBLIC_URL is refused, which also proves the byo path really
    # derived its URL from the env var.
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "http://cp.example.com")
    result = _invoke(["admin", "byo"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "plain HTTP" in result.output


def test_byo_refuses_plain_http_to_the_network(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    result = _invoke(["admin", "byo", "--url", "http://cp.example.com"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "plain HTTP" in result.output


def test_proxy_refuses_non_loopback_bind(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPY_ADMIN_TOKEN", TOKEN)
    result = _invoke(
        ["admin", "byo", "--url", "https://cp.example.com", "--host", "0.0.0.0"],
        monkeypatch,
        tmp_path,
    )
    assert result.exit_code == 1
    assert "loopback" in result.output


def test_bootstrap_derives_tunnel_url_from_recorded_config(monkeypatch, tmp_path):
    from loopy_cli import _admin_bootstrap_url

    monkeypatch.setenv("LOOPY_BOOTSTRAP_INSTANCE_ID", "i-0abc123")
    monkeypatch.setenv("LOOPY_BOOTSTRAP_ENGINE_PORT", "8443")
    assert _admin_bootstrap_url(tmp_path) == "http://localhost:8443/admin"


def test_bootstrap_prints_tunnel_command_and_requires_token(monkeypatch, tmp_path):
    # The tunnel command is printed (with the recorded instance id) before the token gate
    # stops the run — so even a failed invocation tells the user how to reach the dashboard.
    (tmp_path / "loopy.env").write_text(
        "LOOPY_BOOTSTRAP_INSTANCE_ID=i-0abc123\nLOOPY_BOOTSTRAP_ENGINE_PORT=8000\n"
    )
    result = _invoke(["admin", "bootstrap"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "aws ssm start-session --target i-0abc123" in result.output
    assert "LOOPY_ADMIN_TOKEN" in result.output


def test_bootstrap_falls_back_to_placeholder_instance_and_default_port(monkeypatch, tmp_path):
    result = _invoke(["admin", "bootstrap"], monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "--target <instance-id>" in result.output
    assert '"portNumber":["8000"]' in result.output


def test_validate_remote_url():
    assert validate_remote_url("https://cp.example.com/") == "https://cp.example.com"
    assert validate_remote_url("http://127.0.0.1:9100") == "http://127.0.0.1:9100"  # local dev
    with pytest.raises(ValueError, match="plain HTTP"):
        validate_remote_url("http://cp.example.com")
    with pytest.raises(ValueError, match="full http"):
        validate_remote_url("cp.example.com")


# ── the BFF proxy app ───────────────────────────────────────────────────────────────
def _proxy_client(handler) -> TestClient:
    app = create_proxy_app("https://cp.example.com", TOKEN, transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_proxy_injects_bearer_and_forwards_query():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    resp = _proxy_client(handler).get("/api/runs?state=failed&limit=5")
    assert resp.status_code == 200
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["url"] == "https://cp.example.com/api/runs?state=failed&limit=5"


def test_proxy_forwards_under_a_base_path():
    # the derived $LOOPY_PUBLIC_URL/admin base: /api/* must land under /admin/api/*
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    app = create_proxy_app(
        "https://loopy.example.com/admin", TOKEN, transport=httpx.MockTransport(handler)
    )
    assert TestClient(app).get("/api/meta").status_code == 200
    assert seen["url"] == "https://loopy.example.com/admin/api/meta"


def test_proxy_serves_the_ui_shell_locally_and_proxies_static():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/static/app.js"
        return httpx.Response(
            200, content=b"// remote js", headers={"content-type": "text/javascript"}
        )

    client = _proxy_client(handler)
    index = client.get("/")
    assert index.status_code == 200 and b"<html" in index.content.lower()
    static = client.get("/static/app.js")
    assert static.content == b"// remote js"
    assert static.headers["content-type"].startswith("text/javascript")


def test_proxy_maps_remote_401_to_actionable_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "missing or invalid bearer token"})

    resp = _proxy_client(handler).get("/api/meta")
    assert resp.status_code == 401
    assert "LOOPY_ADMIN_TOKEN" in resp.json()["detail"]


def test_proxy_maps_connection_failure_to_actionable_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    resp = _proxy_client(handler).get("/api/meta")
    assert resp.status_code == 502
    assert "https://cp.example.com" in resp.json()["detail"]
