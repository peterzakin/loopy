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
