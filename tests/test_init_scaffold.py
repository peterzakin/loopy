"""`loopy init` scaffolding — the scaffold writes the canonical layout and compiles green.

The load-bearing guarantee is the compile assertion: a freshly initialized project must pass
`loopy compile` with zero diagnostics, so `loopy init` can never scaffold something the rest
of the toolchain rejects.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import loopy_cli
from loopy_cli import app
from loopy_cli.doctor import PLACEHOLDER_ANTHROPIC_KEY
from loopy_cli.scaffold import (
    InvalidProjectName,
    scaffold_project,
    validate_project_name,
)
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.secrets import load_control_plane_env

runner = CliRunner()


def _key_line(target) -> str:
    """The single ANTHROPIC_API_KEY line from a scaffolded project's env_file."""
    lines = (target / "secrets" / "dev.env").read_text().splitlines()
    return next(line for line in lines if line.startswith("ANTHROPIC_API_KEY="))


def test_scaffold_compiles_green(tmp_path):
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    # The starter loop is the documented one: a single CodeTask-triggered workflow.
    assert "codefix" in result.project.workflows


def test_scaffold_writes_canonical_layout(tmp_path):
    target = tmp_path / "demo"
    created = scaffold_project(target, "demo")

    rels = {p.as_posix() for p in created}
    assert {
        "registry.yml",
        "workflows/codefix/open-pr.md",
        "skills/codefix/SKILL.md",
        "sensors/sensors.py",
        "secrets/dev.env",
        "loopy.env",
        ".gitignore",
    } <= rels
    # Secrets and control-plane creds are gitignored from the start.
    gitignore = (target / ".gitignore").read_text()
    assert "loopy.env" in gitignore
    assert "secrets/" in gitignore
    # The project name lands in the header comment via the sentinel replacement.
    assert "# demo" in (target / "registry.yml").read_text()


def test_scaffolded_loopy_env_does_not_block_auth_github(tmp_path):
    """The control-plane template must leave GitHub App vars commented out — an uncommented
    GITHUB_APP_ID would make `loopy auth github` think an App is already configured."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    assert (target / "loopy.env").is_file()
    env = load_control_plane_env(target)
    assert "GITHUB_APP_ID" not in env  # commented placeholder only — auth github stays unblocked
    assert env == {}  # nothing active yet; all placeholders are comments


def test_scaffold_refuses_nonempty_dir(tmp_path):
    target = tmp_path / "demo"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    with pytest.raises(FileExistsError):
        scaffold_project(target, "demo")


def test_scaffold_into_empty_existing_dir_is_ok(tmp_path):
    target = tmp_path / "demo"
    target.mkdir()  # empty — allowed
    scaffold_project(target, "demo")
    assert (target / "registry.yml").is_file()


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "a/b", "a\\b", "-leading", "/abs"])
def test_invalid_project_names_rejected(bad):
    with pytest.raises(InvalidProjectName):
        validate_project_name(bad)


@pytest.mark.parametrize("good", ["demo", "my-proj", "my_proj", "Proj1", "a.b"])
def test_valid_project_names_accepted(good):
    assert validate_project_name(good) == good


def test_validate_strips_surrounding_whitespace():
    assert validate_project_name("  demo  ") == "demo"


# --- the `loopy init` setup wizard ------------------------------------------


def test_ambient_key_offer_rewrites_placeholder_when_confirmed(tmp_path, monkeypatch):
    """A real ANTHROPIC_API_KEY in the environment is written into the env_file on confirm."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")
    assert _key_line(target) == f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey0123456789")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    loopy_cli._offer_ambient_anthropic_key(target)

    assert _key_line(target) == "ANTHROPIC_API_KEY=sk-ant-realkey0123456789"


def test_ambient_key_offer_respects_decline(tmp_path, monkeypatch):
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey0123456789")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    loopy_cli._offer_ambient_anthropic_key(target)

    assert _key_line(target) == f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"


def test_ambient_key_offer_noop_without_env_key(tmp_path, monkeypatch):
    """No key in the environment ⇒ no prompt, no change (and crucially, no crash)."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _boom(*a, **k):  # the prompt must never be reached
        raise AssertionError("should not prompt when no ambient key is present")

    monkeypatch.setattr("typer.confirm", _boom)
    loopy_cli._offer_ambient_anthropic_key(target)

    assert _key_line(target) == f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"


def test_init_non_interactive_writes_placeholder_scaffold(tmp_path, monkeypatch):
    """`--non-interactive` skips every prompt and leaves the verbatim placeholder scaffold."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey0123456789")  # would be offered if asked

    result = runner.invoke(app, ["init", "demo", "--dir", str(tmp_path), "--non-interactive"])

    assert result.exit_code == 0, result.output
    target = tmp_path / "demo"
    assert _key_line(target) == f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"
    # The doctor-backed summary replaces the old static checklist.
    assert "things left" in result.output


def test_init_non_interactive_requires_name(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--non-interactive"])
    assert result.exit_code == 1
    assert "name is required" in result.output
