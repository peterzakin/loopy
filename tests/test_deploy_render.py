"""`loopy deploy render` — the Render.com deploy target."""

from __future__ import annotations

import pytest

# ── secret-file name encoding ───────────────────────────────────────────────────


def test_encode_secret_file_name_flattens_paths():
    from loopy_cli.render import encode_secret_file_name

    assert encode_secret_file_name("loopy.env") == "loopy.env"
    assert encode_secret_file_name("secrets/base.env") == "secrets__base.env"
    assert encode_secret_file_name("sensors/.env") == "sensors__.env"
    assert encode_secret_file_name("a/b/c.env") == "a__b__c.env"


def test_encode_secret_file_name_rejects_ambiguous_paths():
    from loopy_cli.render import encode_secret_file_name

    with pytest.raises(ValueError, match="__"):
        encode_secret_file_name("secrets/my__file.env")


import json

import httpx

# ── RenderClient ────────────────────────────────────────────────────────────────


def _client(handler):
    from loopy_cli.render import RenderClient

    return RenderClient("rnd_test", transport=httpx.MockTransport(handler))


def test_client_sends_bearer_and_unwraps_owners():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"owner": {"id": "own-1", "name": "Vivek"}, "cursor": "c"}])

    owners = _client(handler).owners()
    assert seen["auth"] == "Bearer rnd_test"
    assert seen["url"] == "https://api.render.com/v1/owners"
    assert owners == [{"id": "own-1", "name": "Vivek"}]


def test_client_maps_401_to_mint_hint():
    from loopy_cli.render import RENDER_KEYS_URL, RenderAPIError

    def handler(request):
        return httpx.Response(401, json={"message": "unauthorized"})

    with pytest.raises(RenderAPIError) as excinfo:
        _client(handler).owners()
    assert excinfo.value.status == 401
    assert RENDER_KEYS_URL in excinfo.value.hint


def test_find_service_matches_exact_name_only():
    def handler(request):
        assert request.url.params["name"] == "loopy-demo"
        return httpx.Response(
            200,
            json=[
                {"service": {"id": "srv-other", "name": "loopy-demo-2"}},
                {"service": {"id": "srv-1", "name": "loopy-demo"}},
            ],
        )

    service = _client(handler).find_service("loopy-demo")
    assert service["id"] == "srv-1"


def test_get_service_returns_none_on_404():
    def handler(request):
        return httpx.Response(404, json={"message": "not found"})

    assert _client(handler).get_service("srv-gone") is None


def test_put_env_vars_and_secret_files_send_sorted_arrays():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.put_env_vars("srv-1", {"B": "2", "A": "1"})
    client.put_secret_files("srv-1", {"secrets__base.env": "K=v\n"})
    assert calls[0] == ("PUT", "/v1/services/srv-1/env-vars", [{"key": "A", "value": "1"}, {"key": "B", "value": "2"}])
    assert calls[1] == ("PUT", "/v1/services/srv-1/secret-files", [{"name": "secrets__base.env", "content": "K=v\n"}])


def test_repo_not_connected_hint():
    from loopy_cli.render import RenderAPIError

    def handler(request):
        return httpx.Response(400, json={"message": "repo not found or not connected"})

    with pytest.raises(RenderAPIError) as excinfo:
        _client(handler).create_service({})
    assert "connect" in excinfo.value.hint.lower()


import subprocess

# ── git preflight ───────────────────────────────────────────────────────────────


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_project(tmp_path, *, remote: str | None = "https://github.com/acme/demo.git"):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "registry.yml").write_text("agents: {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    if remote:
        _git(tmp_path, "remote", "add", "origin", remote)
    return tmp_path


def test_normalize_repo_url_matrix():
    from loopy_cli.render import normalize_repo_url

    for raw in (
        "git@github.com:acme/demo.git",
        "https://github.com/acme/demo.git",
        "https://github.com/acme/demo",
        "ssh://git@github.com/acme/demo.git",
    ):
        assert normalize_repo_url(raw) == "https://github.com/acme/demo"
    assert normalize_repo_url("git@gitlab.com:acme/demo.git") == "https://gitlab.com/acme/demo"
    assert normalize_repo_url("https://example.com/acme/demo.git") is None


def test_git_checks_not_a_repo(tmp_path):
    from loopy_cli.render import git_checks

    checks, info = git_checks(tmp_path, None)
    assert [c.key for c in checks] == ["repo"]
    assert not checks[0].ok and not checks[0].warn
    assert "git init" in checks[0].fix
    assert info.repo_url == ""


def test_git_checks_no_usable_remote(tmp_path):
    from loopy_cli.render import git_checks

    checks, info = git_checks(_git_project(tmp_path, remote=None), None)
    by_key = {c.key: c for c in checks}
    assert by_key["repo"].ok
    assert not by_key["remote"].ok and not by_key["remote"].warn
    assert "git remote add origin" in by_key["remote"].fix


def test_git_checks_never_pushed_is_fatal_and_dirty_is_warn(tmp_path):
    from loopy_cli.render import git_checks

    project = _git_project(tmp_path)
    (project / "scratch.txt").write_text("wip")
    checks, info = git_checks(project, None)
    by_key = {c.key: c for c in checks}
    assert info.branch == "main"
    assert info.repo_url == "https://github.com/acme/demo"
    assert not by_key["clean"].ok and by_key["clean"].warn
    assert not by_key["pushed"].ok and not by_key["pushed"].warn
    assert "git push -u origin main" in by_key["pushed"].fix


def test_git_checks_all_green_via_local_bare_remote(tmp_path):
    from loopy_cli.render import git_checks

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project = _git_project(project_dir, remote=None)
    _git(project, "remote", "add", "origin", str(bare))
    _git(project, "push", "-u", "origin", "main")
    checks, info = git_checks(project, None)
    by_key = {c.key: c for c in checks}
    # A local-path remote isn't GitHub/GitLab, so `remote` fails — assert the rest:
    assert by_key["clean"].ok
    assert by_key["pushed"].ok
