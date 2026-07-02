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
    _placeholder_reason,
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
    dev = root / "secrets/base.env"
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

    # A configured App (GITHUB_APP_ID present at the control plane) satisfies git auth, and the
    # scaffold's `provider: daytona` sandbox needs DAYTONA_API_KEY at the control plane too.
    findings = _diagnose(
        root, control_plane_env={"GITHUB_APP_ID": "123456", "DAYTONA_API_KEY": "dtn-real"}
    )
    assert findings == [], [f.message for f in findings]


def test_missing_env_file_is_an_error(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    (root / "secrets/base.env").unlink()  # compile still passes — env_file is a reference only

    findings = _diagnose(root)
    assert any(f.level == "error" and "missing" in f.message for f in findings)


def test_github_token_in_env_file_satisfies_auth(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])  # a declared repo makes git auth relevant
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("# GITHUB_TOKEN=ghp_...", "GITHUB_TOKEN=ghp_real"))

    findings = _diagnose(root)
    # The key placeholder still flags, but git auth is now satisfied (no warn).
    assert not any(f.level == "warn" and "git auth" in f.message for f in findings)


def test_no_auth_warning_when_no_repos_declared(tmp_path):
    # The default scaffold declares no repos — nothing needs cloning, so auth isn't required.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    # Still needs the Daytona key: the default sandbox runs on provider: daytona.
    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert findings == [], [f.message for f in findings]


def test_missing_daytona_key_flags_error(tmp_path):
    # The scaffold sandbox runs on provider: daytona but ships DAYTONA_API_KEY commented out —
    # `run` dies mid-acquire with "DAYTONA_API_KEY is not set"; doctor must catch it up front.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    findings = _diagnose(root)  # no DAYTONA_API_KEY anywhere
    daytona = [f for f in findings if "DAYTONA_API_KEY" in f.message]
    assert len(daytona) == 1
    assert daytona[0].level == "error"
    assert "provider: daytona" in daytona[0].message


def test_daytona_key_present_clears_the_finding(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)

    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert not any("DAYTONA_API_KEY" in f.message for f in findings)


def test_non_daytona_provider_needs_no_daytona_key(tmp_path):
    # A sandbox on a different provider (docker/local) doesn't use Daytona, so its key is moot.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)
    reg = root / "registry.yml"
    reg.write_text(reg.read_text().replace("provider: daytona", "provider: docker"))

    findings = _diagnose(root)  # no DAYTONA_API_KEY, but no daytona sandbox either
    assert not any("DAYTONA_API_KEY" in f.message for f in findings)


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


# --- the placeholder-value heuristic (warn, never error) --------------------


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-...",  # leftover scaffold stub
        "ghp_abc123 # my prod token",  # inline comment folded into a literal dotenv value
        "<your-key-here>",  # angle-bracket template
        "changeme",
        "your-token",
        "REPLACE_ME",
        "example.com",
        "XXXXXXXX",
    ],
)
def test_placeholder_reason_flags_placeholder_values(value):
    assert _placeholder_reason(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-real-key-9f3a2b",  # a plausible real key
        "ghp_16charsofrealtoken",
        "redis://localhost:6379",
        "",  # empty is "unset", not a placeholder
        "  ",
    ],
)
def test_placeholder_reason_passes_real_values(value):
    assert _placeholder_reason(value) is None


def test_inline_comment_in_env_file_is_a_warning(tmp_path):
    # A user pastes a real key but leaves an inline `# comment`; the literal dotenv value now
    # carries the comment and breaks at run. doctor must warn (not error) on it.
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    dev = root / "secrets/base.env"
    dev.write_text(dev.read_text().replace("sk-ant-...", "sk-ant-real # prod key"))

    findings = _diagnose(root)
    hits = [f for f in findings if f.level == "warn" and "ANTHROPIC_API_KEY" in f.message]
    assert len(hits) == 1
    assert "#" in hits[0].message
    # ...and it is a warn, not the hard error reserved for the exact scaffold stub.
    assert not any(f.level == "error" and "ANTHROPIC_API_KEY" in f.message for f in findings)


def test_real_key_produces_no_placeholder_warning(tmp_path):
    # A genuine-looking value must not trip the heuristic (no false-positive warnings).
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key(root)  # writes sk-ant-real-key

    findings = _diagnose(root, control_plane_env={"DAYTONA_API_KEY": "dtn-real"})
    assert findings == [], [f.message for f in findings]


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
