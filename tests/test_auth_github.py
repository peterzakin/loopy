"""`loopy auth github` manifest flow — unit coverage with no live network.

Stubs `github_app._request_json` (the module's single network boundary) and
signs/decodes JWTs with a throwaway RSA key, so nothing here touches GitHub.
"""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from loopy_cli import auth
from loopy_runtime.scm import github_app
from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """A private/public PEM pair for signing and verifying App JWTs in-process."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


# --- manifest shape ---------------------------------------------------------


def test_build_manifest_shape():
    manifest = auth.build_manifest("loopy-acme", "http://127.0.0.1:8765/callback")
    assert manifest["name"] == "loopy-acme"
    assert manifest["url"]  # top-level url is required by GitHub
    assert manifest["redirect_url"] == "http://127.0.0.1:8765/callback"
    assert manifest["public"] is False
    # hook_attributes must be omitted entirely: if present, GitHub requires its `url`,
    # so `{active: false}` alone fails with "url wasn't supplied". No object → no webhook.
    assert "hook_attributes" not in manifest
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
        # lets `loopy webhooks github` create the repo webhooks that deliver Github.* events
        "repository_hooks": "write",
    }


def test_create_app_url_personal_vs_org():
    assert auth.create_app_url(None) == "https://github.com/settings/apps/new"
    assert auth.create_app_url("acme") == "https://github.com/organizations/acme/settings/apps/new"


def test_default_app_name_is_unique_by_construction():
    # GitHub App names are global and bare "loopy" is reserved, so the default carries
    # entropy: same inputs yield different names, and never the bare reserved name.
    a, b = auth.default_app_name(None), auth.default_app_name(None)
    assert a != b
    assert a not in ("loopy", "loopy-")
    assert a.startswith("loopy-")
    assert auth.default_app_name("acme").startswith("loopy-acme-")


def test_submit_page_sets_manifest_via_js_and_round_trips():
    import json
    import re

    manifest = auth.build_manifest("loopy", "http://127.0.0.1:1/callback")
    page = auth.render_submit_page(auth.create_app_url("acme"), manifest, "st&te")
    assert "organizations/acme/settings/apps/new?state=st&amp;te" in page
    assert "name='manifest'" in page  # the field the JS fills
    assert "submit()" in page  # auto-submits via JS

    # The manifest must survive the JS-literal embedding intact, url included — that's
    # the field GitHub rejected when it didn't arrive ("url wasn't supplied").
    literal = re.search(r"\.value = (\".*?\");document", page).group(1)
    recovered = json.loads(json.loads(literal))  # JS string literal → JSON string → dict
    assert recovered == manifest
    assert recovered["url"]


# --- write_control_plane_env: merge / idempotency / no-clobber --------------


def test_write_env_creates_file(tmp_path):
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "42"})
    assert load_control_plane_env(tmp_path) == {"GITHUB_APP_ID": "42"}


def test_write_env_preserves_comments_and_unrelated_keys(tmp_path):
    (tmp_path / "loopy.env").write_text("# infra\nREDIS_URL=redis://x\n\nDAYTONA_API_KEY=abc\n")
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "42"})
    text = (tmp_path / "loopy.env").read_text()
    assert "# infra" in text  # comment kept
    assert "REDIS_URL=redis://x" in text  # unrelated key untouched
    assert "DAYTONA_API_KEY=abc" in text
    assert "GITHUB_APP_ID=42" in text  # new key appended


def test_write_env_replaces_commented_stub_in_place(tmp_path):
    # The scaffold ships commented placeholders; `loopy auth github` should fill them
    # in place, not leave the stub behind and append a duplicate-looking line below.
    (tmp_path / "loopy.env").write_text(
        "# --- GitHub App ---\n"
        "# GITHUB_APP_ID=\n"
        "# GITHUB_APP_PRIVATE_KEY=\n"
        "\n"
        "# DAYTONA_API_KEY=\n"
    )
    write_control_plane_env(
        tmp_path, {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": "pem"}
    )
    text = (tmp_path / "loopy.env").read_text()
    assert "# GITHUB_APP_ID=" not in text  # stub consumed, not left behind
    assert "# GITHUB_APP_PRIVATE_KEY=" not in text
    assert text.count("GITHUB_APP_ID=") == 1  # no duplicate appended at the bottom
    assert text.count("GITHUB_APP_PRIVATE_KEY=") == 1
    assert "# DAYTONA_API_KEY=" in text  # untouched stub for a key we didn't write
    assert load_control_plane_env(tmp_path) == {
        "GITHUB_APP_ID": "42",
        "GITHUB_APP_PRIVATE_KEY": "pem",
    }


def test_write_env_updates_in_place_and_is_idempotent(tmp_path):
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "1", "REDIS_URL": "redis://x"})
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "2"})  # rewrite in place
    first = (tmp_path / "loopy.env").read_text()
    assert load_control_plane_env(tmp_path) == {"GITHUB_APP_ID": "2", "REDIS_URL": "redis://x"}
    assert first.count("GITHUB_APP_ID=") == 1  # not duplicated
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "2"})  # same value again
    assert (tmp_path / "loopy.env").read_text() == first  # no churn


# --- JWT claims -------------------------------------------------------------


def test_app_jwt_claims(rsa_keys):
    private_pem, public_pem = rsa_keys
    creds = github_app.AppCredentials(app_id="98765", private_key_pem=private_pem)
    token = github_app.app_jwt(creds)
    decoded = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert decoded["iss"] == "98765"
    # ~9-min window, backdated 60s for clock skew → 600s total span.
    assert decoded["exp"] - decoded["iat"] == 600


# --- conversion exchange + credential write ---------------------------------


def test_exchange_manifest_code_hits_conversions_endpoint(monkeypatch):
    seen = {}

    def fake(method, url, **kwargs):
        seen["method"], seen["url"] = method, url
        return {"id": 7, "pem": "PEMDATA", "slug": "loopy-acme"}

    monkeypatch.setattr(github_app, "_request_json", fake)
    result = github_app.exchange_manifest_code("the-code")
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/app-manifests/the-code/conversions")
    assert result["slug"] == "loopy-acme"


def test_write_app_credentials_inlines_key_and_gitignores_env(tmp_path):
    conversion = {"id": 1234567, "pem": "-----BEGIN KEY-----\nabc\n-----END\n", "slug": "x"}
    env_path = auth.write_app_credentials(tmp_path, conversion)

    assert env_path == tmp_path / "loopy.env"
    env = load_control_plane_env(tmp_path)
    assert env["GITHUB_APP_ID"] == "1234567"
    # The multi-line PEM is stored single-line with newlines escaped (dotenv has no
    # multi-line values); no path is stored.
    assert "\n" not in env["GITHUB_APP_PRIVATE_KEY"]
    assert env["GITHUB_APP_PRIVATE_KEY"] == "-----BEGIN KEY-----\\nabc\\n-----END\\n"
    assert "GITHUB_APP_PRIVATE_KEY_FILE" not in (tmp_path / "loopy.env").read_text()

    # loopy.env now carries the private key, so it must be gitignored.
    assert "loopy.env" in (tmp_path / ".gitignore").read_text()


def test_credentials_round_trip_restores_multiline_pem(tmp_path):
    pem = "-----BEGIN PRIVATE KEY-----\nMIIBline\nline2==\n-----END PRIVATE KEY-----\n"
    auth.write_app_credentials(tmp_path, {"id": 42, "pem": pem, "slug": "x"})
    env = load_control_plane_env(tmp_path)
    creds = github_app.AppCredentials.from_env(env, root=tmp_path)
    assert creds.app_id == "42"
    assert creds.private_key_pem == pem  # newlines restored on read


def test_from_env_still_supports_key_file(tmp_path):
    # Inline is the default `loopy auth github` writes, but a file path stays valid for
    # production secret mounts.
    (tmp_path / "k.pem").write_text("REALPEM")
    creds = github_app.AppCredentials.from_env(
        {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY_FILE": "k.pem"}, root=tmp_path
    )
    assert creds.private_key_pem == "REALPEM"


def test_from_env_raises_without_id():
    with pytest.raises(github_app.MissingCredentials):
        github_app.AppCredentials.from_env({})


# --- token mint request shape (scoped to repos) -----------------------------


def test_mint_installation_token_scopes_to_repos(monkeypatch, rsa_keys):
    private_pem, _ = rsa_keys
    creds = github_app.AppCredentials(app_id="1", private_key_pem=private_pem)
    captured = {}

    def fake(method, url, *, headers=None, payload=None):
        captured.update(method=method, url=url, headers=headers, payload=payload)
        return {"token": "ghs_xyz", "expires_at": "2026-06-18T12:00:00Z"}

    monkeypatch.setattr(github_app, "_request_json", fake)
    result = github_app.mint_installation_token(creds, 4815, repositories=["api", "web"])

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/app/installations/4815/access_tokens")
    assert captured["payload"] == {"repositories": ["api", "web"]}  # scoped, least privilege
    assert captured["headers"]["Authorization"].startswith("Bearer ")  # App JWT auth
    assert result["token"] == "ghs_xyz"


def test_mint_installation_token_unscoped_sends_no_body(monkeypatch, rsa_keys):
    private_pem, _ = rsa_keys
    creds = github_app.AppCredentials(app_id="1", private_key_pem=private_pem)
    captured = {}

    def fake(method, url, *, headers=None, payload=None):
        captured["payload"] = payload
        return {"token": "t"}

    monkeypatch.setattr(github_app, "_request_json", fake)
    github_app.mint_installation_token(creds, 1)
    assert captured["payload"] is None  # whole-installation token when unscoped


def test_list_installation_repositories_paginates(monkeypatch):
    # An "all repositories" install on a busy account spans many pages; the helper
    # must accumulate every page, not just the first 30 GitHub returns by default.
    # Otherwise the doctor preflight flags reachable repos as "not selected".
    pages = {
        1: {"total_count": 150, "repositories": [{"full_name": f"me/r{i}"} for i in range(100)]},
        2: {
            "total_count": 150,
            "repositories": [{"full_name": f"me/r{i}"} for i in range(100, 150)],
        },
    }
    calls = []

    def fake(method, url, *, headers=None, payload=None):
        calls.append(url)
        page = 2 if "page=2" in url else 1
        return pages[page]

    monkeypatch.setattr(github_app, "_request_json", fake)
    result = github_app.list_installation_repositories("t")

    assert result["total_count"] == 150
    assert len(result["repositories"]) == 150
    assert result["repositories"][-1]["full_name"] == "me/r149"
    assert any("per_page=100" in url for url in calls)  # asks for the larger page
    assert len(calls) == 2  # walked both pages, then stopped


def test_list_installation_repositories_single_page_stops(monkeypatch):
    # A small install fits on one page — don't fetch a needless second page.
    calls = []

    def fake(method, url, *, headers=None, payload=None):
        calls.append(url)
        return {"total_count": 2, "repositories": [{"full_name": "me/a"}, {"full_name": "me/b"}]}

    monkeypatch.setattr(github_app, "_request_json", fake)
    result = github_app.list_installation_repositories("t")

    assert len(result["repositories"]) == 2
    assert len(calls) == 1


# --- block-until-installed wait ---------------------------------------------


def test_wait_for_installation_polls_until_installed(monkeypatch, tmp_path):
    # Creating an App does not install it; the wait must keep polling until an
    # installation appears, not check once and give up.
    monkeypatch.setattr(auth, "_load_creds", lambda root: object())
    responses = [[], [], [{"id": 99}]]  # empty twice, then installed
    calls = {"n": 0}

    def fake_list(creds, **kwargs):
        out = responses[calls["n"]]
        calls["n"] += 1
        return out

    monkeypatch.setattr(github_app, "list_installations", fake_list)
    slept: list[float] = []
    result = auth._wait_for_installation(
        tmp_path, sleep=slept.append, monotonic=lambda: 0.0
    )
    assert result == [{"id": 99}]
    assert calls["n"] == 3  # polled until non-empty
    assert len(slept) == 2  # slept between the empty polls


def test_run_github_auth_opens_install_page(monkeypatch, tmp_path):
    # After the manifest flow creates the App, the install page must auto-open in the
    # browser (unless --no-browser) so the user lands straight on the repo-picker.
    import webbrowser

    monkeypatch.setattr(auth, "obtain_manifest_code", lambda **kwargs: "code123")
    monkeypatch.setattr(
        github_app,
        "exchange_manifest_code",
        lambda code: {"id": 7, "pem": "PEM", "slug": "loopy-abcd"},
    )
    monkeypatch.setattr(auth, "_wait_for_installation", lambda root: None)
    monkeypatch.setattr(auth, "_verify", lambda root: None)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    auth.run_github_auth(root=tmp_path)

    assert opened == ["https://github.com/apps/loopy-abcd/installations/new"]


def test_run_github_auth_no_browser_skips_open(monkeypatch, tmp_path):
    # --no-browser prints the URL but must not shell out to a browser (headless/CI/SSH).
    import webbrowser

    monkeypatch.setattr(auth, "obtain_manifest_code", lambda **kwargs: "code123")
    monkeypatch.setattr(
        github_app,
        "exchange_manifest_code",
        lambda code: {"id": 7, "pem": "PEM", "slug": "loopy-abcd"},
    )
    monkeypatch.setattr(auth, "_wait_for_installation", lambda root: None)
    monkeypatch.setattr(auth, "_verify", lambda root: None)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    auth.run_github_auth(root=tmp_path, no_browser=True)

    assert opened == []


def test_wait_for_installation_times_out(monkeypatch, tmp_path):
    # Never installed → the wait gives up at the deadline and returns None (the caller
    # then points the user at `loopy auth github`) rather than blocking forever.
    monkeypatch.setattr(auth, "_load_creds", lambda root: object())
    monkeypatch.setattr(github_app, "list_installations", lambda creds, **k: [])
    clock = iter([0.0, 0.0, 999.0])  # deadline check, first poll, then past the deadline
    result = auth._wait_for_installation(
        tmp_path, timeout=10, sleep=lambda _s: None, monotonic=lambda: next(clock)
    )
    assert result is None


# ── `loopy auth github`: status when registered, else create ─────────────────


def _invoke(args):
    from typer.testing import CliRunner

    from loopy_cli import app

    return CliRunner().invoke(app, args)


def test_command_shows_status_when_registered(monkeypatch, tmp_path):
    """Stored App creds make `loopy auth github` verify + report, not re-run the flow."""
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": "PEM"})
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_github_auth", lambda **_k: ran.__setitem__("auth", True))
    monkeypatch.setattr(auth, "_verify", lambda root: None)

    result = _invoke(["auth", "github", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "42" in result.output  # reported the stored App id
    assert "--force" in result.output  # surfaced how to run the flow again
    assert ran["auth"] is False  # showed status, did not start the manifest flow


def test_command_starts_flow_when_unregistered(monkeypatch, tmp_path):
    """With no stored App, `loopy auth github` runs the manifest flow."""
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_github_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "github", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert ran["auth"] is True


def test_command_force_reruns_flow_even_when_registered(monkeypatch, tmp_path):
    """--force overrides the status short-circuit and re-runs the manifest flow."""
    write_control_plane_env(tmp_path, {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": "PEM"})
    ran = {"auth": False}
    monkeypatch.setattr(auth, "run_github_auth", lambda **_k: ran.__setitem__("auth", True))

    result = _invoke(["auth", "github", "--root", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert ran["auth"] is True
