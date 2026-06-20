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
    diagnose,
)
from loopy_cli.scaffold import scaffold_project
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.secrets import _parse_dotenv, load_control_plane_env


def _diagnose(root, *, control_plane_env=None, is_tracked=None):
    """Wire `diagnose` exactly as the CLI does, but with an explicit control-plane env so the
    test never inherits a real `GITHUB_APP_ID` from the host environment. `is_tracked` defaults
    to "nothing tracked" so the git guardrail stays out of the way unless a test opts in."""
    result = compile_project(root)
    assert result.project is not None, [d.render() for d in result.diagnostics.items]

    def read_env(rel: str):
        path = root / rel
        return _parse_dotenv(path.read_text()) if path.is_file() else None

    merged = dict(control_plane_env or {})
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return diagnose(
        result.project.registry,
        read_env=read_env,
        control_plane_env=merged,
        is_tracked=is_tracked or (lambda rel: False),
    )


def _fix_key_and_repo(root) -> None:
    """Replace the two scaffold placeholders that aren't auth-related."""
    dev = root / "secrets/dev.env"
    dev.write_text(dev.read_text().replace("sk-ant-...", "sk-ant-real-key"))
    reg = root / "registry.yml"
    reg.write_text(reg.read_text().replace("octocat/Hello-World", "me/my-fork"))


def test_fresh_scaffold_flags_all_three_gaps(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")

    findings = _diagnose(root)
    errors = {f.message for f in findings if f.level == "error"}
    warns = {f.message for f in findings if f.level == "warn"}

    assert any("ANTHROPIC_API_KEY" in m for m in errors)
    assert any("repos:" in m for m in errors)
    assert any("git auth" in m for m in warns)


def test_clean_project_has_no_findings(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key_and_repo(root)

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
    scaffold_project(root, "demo")
    dev = root / "secrets/dev.env"
    dev.write_text(dev.read_text().replace("# GITHUB_TOKEN=ghp_...", "GITHUB_TOKEN=ghp_real"))

    findings = _diagnose(root)
    # The key/repo placeholders still flag, but git auth is now satisfied (no warn).
    assert not any(f.level == "warn" and "git auth" in f.message for f in findings)


def test_no_auth_warning_when_no_repos_declared(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key_and_repo(root)
    # Drop the repos: line entirely — nothing needs cloning, so auth isn't required.
    reg = root / "registry.yml"
    lines = [ln for ln in reg.read_text().splitlines() if "repos:" not in ln]
    reg.write_text("\n".join(lines) + "\n")

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


# --- guardrail: env_file committed to git -----------------------------------


def test_tracked_env_file_warns(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key_and_repo(root)

    # Pretend secrets/dev.env is tracked; everything else (the scaffold gitignores secrets/) isn't.
    findings = _diagnose(
        root,
        control_plane_env={"GITHUB_APP_ID": "123456"},  # satisfy git auth so only this warns
        is_tracked=lambda rel: rel == "secrets/dev.env",
    )
    tracked = [f for f in findings if "tracked by git" in f.message]
    assert len(tracked) == 1
    assert tracked[0].level == "warn"
    assert "secrets/dev.env" in tracked[0].message
    assert "git rm --cached secrets/dev.env" in (tracked[0].hint or "")


def test_untracked_env_file_does_not_warn(tmp_path):
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    _fix_key_and_repo(root)

    # Nothing tracked (the default scaffold state) → no tracking warning at all.
    findings = _diagnose(
        root,
        control_plane_env={"GITHUB_APP_ID": "123456"},
        is_tracked=lambda rel: False,
    )
    assert not any("tracked by git" in f.message for f in findings)


def test_is_git_tracked_detects_committed_file(tmp_path):
    """The real git-backed helper the CLI injects: tracked ⇒ True, untracked/ignored ⇒ False."""
    import subprocess

    from loopy_cli import _is_git_tracked

    root = tmp_path / "repo"
    root.mkdir()

    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "secrets").mkdir()
    (root / "secrets" / "dev.env").write_text("ANTHROPIC_API_KEY=sk-ant-live\n")
    (root / "untracked.env").write_text("X=1\n")
    run("add", "secrets/dev.env")
    run("commit", "-m", "add env")

    assert _is_git_tracked(root, "secrets/dev.env") is True
    assert _is_git_tracked(root, "untracked.env") is False
    assert _is_git_tracked(root, "does/not/exist.env") is False


def test_is_git_tracked_outside_repo_is_false(tmp_path):
    """A non-repo project (or no git) has nothing tracked — degrade to False, never raise."""
    from loopy_cli import _is_git_tracked

    assert _is_git_tracked(tmp_path, "secrets/dev.env") is False
