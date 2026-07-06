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


_REGISTRY = (
    "sandboxes:\n"
    "  BaseSandbox:\n"
    "    provider: daytona\n"
    "    env_file: secrets/base.env\n"
    "agents:\n"
    "  Worker: { model: claude-sonnet-4-6, harness: claude-code, sandbox: BaseSandbox }\n"
)


def _loopy_project(tmp_path):
    (tmp_path / "registry.yml").write_text(_REGISTRY)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "base.env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    (tmp_path / "loopy.env").write_text(
        "DAYTONA_API_KEY=dt-real\nLOOPY_ADMIN_TOKEN=loopy_sk_admin\nRENDER_API_KEY=rnd_k\n"
    )
    return tmp_path


def _compiled_manifest(project) -> "Path":
    from typer.testing import CliRunner

    from loopy_cli import app

    manifest = project / "manifest.json"
    # `--out` defaults to a CWD-relative "manifest.json", not one relative to the project
    # directory being compiled — pass it explicitly so the manifest lands next to the project
    # regardless of the test runner's working directory.
    result = CliRunner().invoke(app, ["compile", str(project), "--out", str(manifest)])
    assert result.exit_code == 0, result.output
    return manifest


# ── project preflight ───────────────────────────────────────────────────────────


def test_project_checks_green_project_still_needs_dockerfile(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert by_key["loopy_env"].ok
    assert by_key["admin_token"].ok
    assert by_key["env_files"].ok
    assert not by_key["dockerfile"].ok and not by_key["dockerfile"].warn
    assert "loopy dockerfile" in by_key["dockerfile"].fix


def test_project_checks_flags_missing_env_file_and_token(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    (project / "secrets" / "base.env").unlink()
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt-real\n")
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert not by_key["env_files"].ok and not by_key["env_files"].warn
    assert "secrets/base.env" in by_key["env_files"].fix
    assert not by_key["admin_token"].ok and by_key["admin_token"].warn


def test_project_checks_stale_dockerfile_pin_is_warn(tmp_path):
    from loopy_cli.render import project_checks

    project = _loopy_project(tmp_path)
    manifest = _compiled_manifest(project)
    (project / "Dockerfile").write_text("FROM python:3.12-slim\nRUN pip install loopy-computer==0.0.0\n")
    by_key = {c.key: c for c in project_checks(project, manifest)}
    assert not by_key["dockerfile"].ok and by_key["dockerfile"].warn


def test_print_checks_reports_fatal_and_warn():
    from loopy_cli.render import Check, print_checks

    fatal, warn = print_checks(
        [
            Check("a", "fine", True),
            Check("b", "soft", False, warn=True, fix="do the soft thing"),
            Check("c", "hard", False, fix="do the hard thing"),
        ]
    )
    assert fatal is True and warn is True


def test_has_cron_workflows(tmp_path):
    from loopy_cli.render import has_cron_workflows

    project = _loopy_project(tmp_path)
    workflow = project / "workflows" / "tick"
    workflow.mkdir(parents=True)
    (workflow / "step.md").write_text(
        "---\non: cron(\"0 * * * *\")\nagent: Worker\n---\nDo the hourly thing.\n"
    )
    manifest = _compiled_manifest(project)
    assert has_cron_workflows(manifest) is True

    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    plain = _loopy_project(plain_root)
    assert has_cron_workflows(_compiled_manifest(plain)) is False


# ── wizard ──────────────────────────────────────────────────────────────────────


def _fake_client(owners):
    class FakeClient:
        def __init__(self, key):
            self.key = key

        def owners(self):
            from loopy_cli.render import RenderAPIError

            if self.key == "rnd_bad":
                raise RenderAPIError(401, "unauthorized", "mint a new one")
            return owners

    return FakeClient


def test_connect_uses_recorded_key_and_sole_owner(tmp_path, monkeypatch, capsys):
    from loopy_cli import render as render_mod

    project = _loopy_project(tmp_path)  # loopy.env already has RENDER_API_KEY=rnd_k
    monkeypatch.setattr(render_mod, "_client_factory", _fake_client([{"id": "own-1", "name": "V"}]))
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    client, owner = render_mod.connect(project)
    assert client.key == "rnd_k"
    assert owner["id"] == "own-1"


def test_connect_headless_without_key_exits_naming_the_fix(tmp_path, monkeypatch):
    import typer

    from loopy_cli import render as render_mod

    project = _loopy_project(tmp_path)
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt\n")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    with pytest.raises(typer.Exit):
        render_mod.connect(project)


def test_connect_prompts_writes_key_back(tmp_path, monkeypatch):
    from loopy_cli import render as render_mod
    from loopy_runtime.secrets import load_control_plane_env

    project = _loopy_project(tmp_path)
    (project / "loopy.env").write_text("DAYTONA_API_KEY=dt\n")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setattr(render_mod, "_client_factory", _fake_client([{"id": "own-1", "name": "V"}]))
    monkeypatch.setattr(render_mod, "_interactive", lambda: True)
    answers = iter(["rnd_new"])
    monkeypatch.setattr(render_mod.typer, "prompt", lambda *a, **k: next(answers))
    client, owner = render_mod.connect(project)
    assert load_control_plane_env(project)["RENDER_API_KEY"] == "rnd_new"


def test_choose_plan_flag_wins_and_headless_requires_it(monkeypatch):
    import typer

    from loopy_cli import render as render_mod

    assert render_mod.choose_plan(has_cron=True, plan_flag="standard") == "standard"
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    with pytest.raises(typer.Exit):
        render_mod.choose_plan(has_cron=False, plan_flag=None)


def test_choose_plan_prompt_defaults_to_starter(monkeypatch, capsys):
    from loopy_cli import render as render_mod

    monkeypatch.setattr(render_mod, "_interactive", lambda: True)
    monkeypatch.setattr(render_mod.typer, "prompt", lambda *a, **k: k.get("default", "2"))
    assert render_mod.choose_plan(has_cron=True, plan_flag=None) == "starter"
    out = capsys.readouterr().out
    assert "WILL miss" in out  # cron-aware annotation


# ── the command ─────────────────────────────────────────────────────────────────


def test_build_create_payload_shape():
    from loopy_cli.render import RepoInfo, build_create_payload

    payload = build_create_payload(
        name="loopy-demo",
        owner_id="own-1",
        info=RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        plan="starter",
        region="oregon",
    )
    assert payload == {
        "type": "web_service",
        "name": "loopy-demo",
        "ownerId": "own-1",
        "repo": "https://github.com/acme/demo",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "runtime": "docker",
            "plan": "starter",
            "region": "oregon",
            "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
        },
    }


def test_build_create_payload_with_disk():
    from loopy_cli.render import RepoInfo, build_create_payload

    payload = build_create_payload(
        name="loopy-demo",
        owner_id="own-1",
        info=RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        plan="starter",
        region="oregon",
        disk_gb=5,
    )
    # The generated Dockerfile's start command uses /state/state.db when /state exists.
    assert payload["serviceDetails"]["disk"] == {"name": "state", "mountPath": "/state", "sizeGB": 5}


def test_poll_deploy_returns_terminal_status():
    from loopy_cli.render import _poll_deploy

    statuses = iter(["build_in_progress", "update_in_progress", "live"])

    class FakeClient:
        def get_deploy(self, service_id, deploy_id):
            return {"id": deploy_id, "status": next(statuses)}

    final = _poll_deploy(FakeClient(), "srv-1", "dep-1", sleep=lambda s: None)
    assert final == "live"


class _ScriptedClient:
    """A full fake for the command test: records calls, plays a create-flow script."""

    def __init__(self, key):
        self.key = key
        self.calls = []

    def owners(self):
        return [{"id": "own-1", "name": "V"}]

    def get_service(self, service_id):
        return None

    def find_service(self, name):
        self.calls.append(("find", name))
        return None

    def create_service(self, payload):
        self.calls.append(("create", payload))
        return {
            "id": "srv-9",
            "name": payload["name"],
            "serviceDetails": {"url": "https://loopy-demo.onrender.com"},
        }

    def put_env_vars(self, service_id, env):
        self.calls.append(("env", service_id, dict(env)))

    def put_secret_files(self, service_id, files):
        self.calls.append(("secrets", service_id, dict(files)))

    def trigger_deploy(self, service_id):
        self.calls.append(("deploy", service_id))
        return {"id": "dep-1", "status": "created"}

    def get_deploy(self, service_id, deploy_id):
        return {"id": deploy_id, "status": "live"}


def test_deploy_render_happy_path_create(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod
    from loopy_cli.render import Check, RepoInfo
    from loopy_runtime.secrets import load_control_plane_env

    project = _loopy_project(tmp_path)
    (project / "Dockerfile").write_text("# Generated by `loopy dockerfile`\n")
    scripted = {}

    def factory(key):
        scripted["client"] = _ScriptedClient(key)
        return scripted["client"]

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    monkeypatch.setattr(
        render_mod,
        "git_checks",
        lambda root, branch: (
            [Check("repo", "git", True), Check("remote", "origin", True)],
            RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        ),
    )
    # Pin: the Dockerfile stub above doesn't carry the real version pin — silence the warn.
    monkeypatch.setattr(
        render_mod, "project_checks", lambda root, manifest: [Check("ok", "project", True)]
    )
    monkeypatch.setattr(render_mod, "wait_until_serving", lambda url, **kw: True)
    monkeypatch.setattr(render_mod, "_register_webhooks", lambda root, url: ["registered"])

    result = CliRunner().invoke(
        app, ["deploy", "render", str(project), "--root", str(project), "--plan", "free"]
    )
    assert result.exit_code == 0, result.output

    client = scripted["client"]
    kinds = [c[0] for c in client.calls]
    assert kinds == ["find", "create", "env", "secrets", "deploy"]
    env_pushed = client.calls[2][2]
    assert env_pushed["DAYTONA_API_KEY"] == "dt-real"
    assert "RENDER_API_KEY" not in env_pushed
    # loopy.env is NOT uploaded: env vars already carry the control plane, and the file
    # holds RENDER_API_KEY itself — the deployed service must never receive that.
    assert client.calls[3][2] == {"secrets__base.env": "ANTHROPIC_API_KEY=sk-ant-x\n"}
    control = load_control_plane_env(project)
    assert control["LOOPY_PUBLIC_URL"] == "https://loopy-demo.onrender.com"
    assert control["LOOPY_RENDER_SERVICE_ID"] == "srv-9"
    assert "loopy deploy render --destroy" in result.output


def test_deploy_render_update_path_uses_recorded_id(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from loopy_cli import app
    from loopy_cli import render as render_mod
    from loopy_cli.render import Check, RepoInfo

    project = _loopy_project(tmp_path)
    (project / "Dockerfile").write_text("# Generated by `loopy dockerfile`\n")
    (project / "loopy.env").write_text(
        "DAYTONA_API_KEY=dt-real\nRENDER_API_KEY=rnd_k\nLOOPY_RENDER_SERVICE_ID=srv-9\n"
    )

    class UpdateClient(_ScriptedClient):
        def get_service(self, service_id):
            self.calls.append(("get", service_id))
            return {
                "id": "srv-9",
                "name": "loopy-demo",
                "serviceDetails": {"url": "https://loopy-demo.onrender.com", "plan": "free"},
            }

    scripted = {}

    def factory(key):
        scripted["client"] = UpdateClient(key)
        return scripted["client"]

    monkeypatch.setattr(render_mod, "_client_factory", factory)
    monkeypatch.setattr(render_mod, "_interactive", lambda: False)
    monkeypatch.setattr(
        render_mod,
        "git_checks",
        lambda root, branch: (
            [Check("repo", "git", True)],
            RepoInfo(branch="main", repo_url="https://github.com/acme/demo"),
        ),
    )
    monkeypatch.setattr(
        render_mod, "project_checks", lambda root, manifest: [Check("ok", "project", True)]
    )
    monkeypatch.setattr(render_mod, "wait_until_serving", lambda url, **kw: True)
    monkeypatch.setattr(render_mod, "_register_webhooks", lambda root, url: [])

    result = CliRunner().invoke(app, ["deploy", "render", str(project), "--root", str(project)])
    assert result.exit_code == 0, result.output
    kinds = [c[0] for c in scripted["client"].calls]
    # Recorded id → straight to the update path: no find, no create, no plan prompt.
    assert kinds == ["get", "env", "secrets", "deploy"]
    assert "updating in place" in result.output
