"""`loopy doctor` — the first-run preflight that catches runnable-vs-valid gaps.

The point of `doctor` is the inverse of `loopy compile`: a freshly scaffolded project compiles
green yet can't run, and these tests pin that exact gap — the placeholder API key, the
unpushable starter repo, and missing git auth — plus the clean state where all three are fixed.
The logic under test (`diagnose`) is pure; the tests drive it through real compiled projects.
"""

from __future__ import annotations

import pytest

from loopy_cli.doctor import (
    PLACEHOLDER_REPO,
    _repo_slug,
    check_repo_access,
    diagnose,
)
from loopy_cli.scaffold import scaffold_project
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.scm import github_app
from loopy_runtime.secrets import _parse_dotenv, load_control_plane_env


def _diagnose(root, *, control_plane_env=None):
    """Wire `diagnose` exactly as the CLI does, but with an explicit control-plane env so the
    test never inherits a real `GITHUB_APP_ID` from the host environment."""
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]

    def read_env(rel: str):
        path = root / rel
        return _parse_dotenv(path.read_text()) if path.is_file() else None

    merged = dict(control_plane_env or {})
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return diagnose(result.project.registry, read_env=read_env, control_plane_env=merged)


def _fix_key(root) -> None:
    """Replace the placeholder model key — the one scaffold gap unrelated to repos/auth."""
    dev = root / "secrets/dev.env"
    dev.write_text(dev.read_text().replace("sk-ant-...", "sk-ant-real-key"))


def test_starter_repo_scaffold_flags_all_three_gaps(tmp_path):
    # A project pointed at the unpushable starter repo trips every gap: placeholder key, the
    # repo itself, and (because it now declares a repo) missing git auth.
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["octocat/Hello-World"])

    findings = _diagnose(root)
    errors = {f.message for f in findings if f.level == "error"}
    warns = {f.message for f in findings if f.level == "warn"}

    assert any("ANTHROPIC_API_KEY" in m for m in errors)
    assert any("repos:" in m for m in errors)
    assert any("git auth" in m for m in warns)


def test_blank_repo_scaffold_flags_only_the_model_key(tmp_path):
    # The default scaffold ships no repo: a valid workflow orchestrator. The only gap is the
    # placeholder model key — there's no repo to push to, so no repo gap and no git auth to wire.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")

    findings = _diagnose(root)
    assert any(f.level == "error" and "ANTHROPIC_API_KEY" in f.message for f in findings)
    assert not any("repos:" in f.message for f in findings)
    assert not any("git auth" in f.message for f in findings)


def test_clean_project_has_no_findings(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/my-fork"])
    _fix_key(root)

    # A configured App (GITHUB_APP_ID present at the control plane) satisfies git auth.
    findings = _diagnose(root, control_plane_env={"GITHUB_APP_ID": "123456"})
    assert findings == [], [f.message for f in findings]


def test_missing_env_file_is_an_error(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    (root / "secrets/dev.env").unlink()  # compile still passes — env_file is a reference only

    findings = _diagnose(root)
    assert any(f.level == "error" and "missing" in f.message for f in findings)


def test_github_token_in_env_file_satisfies_auth(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])  # a declared repo makes git auth relevant
    dev = root / "secrets/dev.env"
    dev.write_text(dev.read_text().replace("# GITHUB_TOKEN=ghp_...", "GITHUB_TOKEN=ghp_real"))

    findings = _diagnose(root)
    # The key placeholder still flags, but git auth is now satisfied (no warn).
    assert not any(f.level == "warn" and "git auth" in f.message for f in findings)


def test_no_auth_warning_when_no_repos_declared(tmp_path):
    # The default scaffold declares no repos — nothing needs cloning, so auth isn't required.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    findings = _diagnose(root)
    assert findings == [], [f.message for f in findings]


@pytest.mark.parametrize(
    "url",
    [
        "octocat/Hello-World",
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World.git",
        "git@github.com:octocat/Hello-World.git",
    ],
)
def test_repo_slug_normalizes_to_starter(url):
    assert _repo_slug(url) == PLACEHOLDER_REPO


# --- check_repo_access: the live install/repo-reachability preflight --------


def _registry(tmp_path, repos):
    """A compiled registry for a scaffold pointed at `repos` (drives the live check)."""
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=repos)
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]
    return result.project.registry


def _stub_installation(monkeypatch, *, installations, reachable):
    """Stub the App's installation lookup + the repos a minted token can reach."""
    monkeypatch.setattr(github_app, "list_installations", lambda creds, **k: installations)
    monkeypatch.setattr(github_app, "mint_installation_token", lambda creds, i, **k: {"token": "t"})
    monkeypatch.setattr(
        github_app,
        "list_installation_repositories",
        lambda token, **k: {"repositories": [{"full_name": n} for n in reachable]},
    )


def test_check_repo_access_passes_when_repos_reachable(tmp_path, monkeypatch):
    registry = _registry(tmp_path, ["me/app", "me/lib"])
    _stub_installation(monkeypatch, installations=[{"id": 7}], reachable=["me/app", "me/lib"])
    assert check_repo_access(registry, object()) == []


def test_check_repo_access_flags_repo_not_in_selection(tmp_path, monkeypatch):
    # The App is installed, but a declared repo isn't among its selected repositories — the
    # exact gap that otherwise only surfaced as a clone 403 at run time.
    registry = _registry(tmp_path, ["me/app", "me/missing"])
    _stub_installation(monkeypatch, installations=[{"id": 7}], reachable=["me/app"])
    findings = check_repo_access(registry, object())
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "me/missing" in findings[0].message
    assert "selected repositories" in findings[0].message


def test_check_repo_access_flags_uninstalled_app(tmp_path, monkeypatch):
    registry = _registry(tmp_path, ["me/app"])
    monkeypatch.setattr(github_app, "list_installations", lambda creds, **k: [])
    findings = check_repo_access(registry, object())
    assert findings[0].level == "error"
    assert "not installed" in findings[0].message


def test_check_repo_access_skips_when_no_repos(tmp_path, monkeypatch):
    # An orchestrator project clones nothing, so there's nothing to verify — and no network call.
    registry = _registry(tmp_path, [])

    def boom(*_a, **_k):
        raise AssertionError("should not call GitHub when no repos are declared")

    monkeypatch.setattr(github_app, "list_installations", boom)
    assert check_repo_access(registry, object()) == []


def test_check_repo_access_degrades_to_warn_on_error(tmp_path, monkeypatch):
    # A GitHub/network error must not fail the whole preflight — it degrades to one warn.
    registry = _registry(tmp_path, ["me/app"])

    def boom(creds, **_k):
        raise github_app.GitHubAppError("boom")

    monkeypatch.setattr(github_app, "list_installations", boom)
    findings = check_repo_access(registry, object())
    assert len(findings) == 1 and findings[0].level == "warn"


def test_diagnose_runnability_backstops_unexpected_live_check_crash(tmp_path, monkeypatch):
    # A diagnostic command must never surface a raw traceback. Even if the live check throws
    # something it doesn't anticipate, _diagnose_runnability degrades it to a warn so a flaky
    # API response can't make a fine scaffold read as corrupted.
    import loopy_cli
    from loopy_cli import doctor

    monkeypatch.delenv("GITHUB_APP_ID", raising=False)  # don't inherit a real App from the host
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])
    _fix_key(root)
    # Configure an App so the live check runs (and so its creds load cleanly).
    (root / "loopy.env").write_text(
        "GITHUB_APP_ID=123456\nGITHUB_APP_PRIVATE_KEY=-----BEGIN KEY-----\\nabc\\n-----END\\n\n"
    )

    def boom(registry, creds):
        raise RuntimeError("unexpected mid-stream truncation")

    monkeypatch.setattr(doctor, "check_repo_access", boom)
    result = compile_project(root)
    assert result.project is not None
    findings = loopy_cli._diagnose_runnability(root, result.project)

    warns = [f for f in findings if f.level == "warn"]
    assert any("couldn't verify repo access" in f.message for f in warns)
    assert not any(f.level == "error" for f in findings)  # the scaffold itself is fine
