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
    lines = (target / "secrets" / "base.env").read_text().splitlines()
    return next(line for line in lines if line.startswith("ANTHROPIC_API_KEY="))


def test_coding_scaffold_compiles_green(tmp_path):
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    # With a repo, the starter is the coding loop: a single CodeTask-triggered workflow.
    assert "codefix" in result.project.workflows


def test_orchestrator_scaffold_compiles_green(tmp_path):
    """No repo ⇒ the non-coding starter: a Note → summary loop that compiles and runs as-is."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    # A real, keepable workflow — not an empty shell or a placeholder to fix.
    assert "summarize" in result.project.workflows
    # No repo declared, and crucially never the old unpushable placeholder.
    assert result.project.registry.sandboxes["BaseSandbox"].repos == []
    assert "octocat" not in (target / "registry.yml").read_text().lower()


def test_scaffold_declares_an_agent_per_harness(tmp_path):
    """No built-in agents: both starters write the explicit yaml for all three harnesses,
    so switching a step's harness is editing a visible declaration, never discovering one."""
    for label, repos in (("coding", ["me/app"]), ("orch", None)):
        target = tmp_path / label
        scaffold_project(target, "demo", repos=repos)
        result = compile_project(target)
        assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
        agents = result.project.registry.agents
        assert {a.harness for a in agents.values()} == {"claude-code", "codex", "opencode"}
        # The opencode agent names the same bare id as the others; the harness sugars it
        # into opencode's provider/model form at run time.
        assert agents["OpenCode"].model == "claude-sonnet-4-6"


def test_scaffold_writes_named_repos(tmp_path):
    """Repos named at init land verbatim in the BaseSandbox sandbox's `repos:` and compile green."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app", "me/other"])

    registry = (target / "registry.yml").read_text()
    assert "repos: [me/app, me/other]" in registry

    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    repos = [r.url for r in result.project.registry.sandboxes["BaseSandbox"].repos]
    assert repos == ["me/app", "me/other"]


def test_coding_scaffold_writes_canonical_layout(tmp_path):
    target = tmp_path / "demo"
    created = scaffold_project(target, "demo", repos=["me/app"])

    rels = {p.as_posix() for p in created}
    assert {
        "registry.yml",
        "workflows/codefix/open-pr.md",
        "skills/codefix/SKILL.md",
        "sensors/sensors.py",
        "secrets/base.env",
        "loopy.env",
        ".gitignore",
    } <= rels
    # Secrets and control-plane creds are gitignored from the start.
    gitignore = (target / ".gitignore").read_text()
    assert "loopy.env" in gitignore
    assert "secrets/" in gitignore
    # The project name lands in the header comment via the sentinel replacement.
    assert "# demo" in (target / "registry.yml").read_text()


def test_orchestrator_scaffold_writes_summarize_layout(tmp_path):
    target = tmp_path / "demo"
    created = scaffold_project(target, "demo")

    rels = {p.as_posix() for p in created}
    assert {
        "registry.yml",
        "workflows/summarize/summarize.md",
        "skills/summarize/SKILL.md",
        "sensors/sensors.py",
        "secrets/base.env",
        "loopy.env",
        ".gitignore",
    } <= rels
    # No codefix/PR files leak into the non-coding starter.
    assert not any("codefix" in r for r in rels)
    assert "# demo" in (target / "registry.yml").read_text()


def test_scaffolded_loopy_env_does_not_block_auth_github(tmp_path):
    """The coding control-plane template must leave GitHub App vars commented out — an uncommented
    GITHUB_APP_ID would make `loopy auth github` think an App is already configured."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

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


def test_scaffold_preserves_creds_from_prior_auth(tmp_path):
    """`loopy init` runs `loopy auth github` before scaffolding, so a target may already hold a
    loopy.env with real App creds. Scaffolding must keep those creds (merged under its template's
    comments), not clobber them with the commented placeholder."""
    from loopy_runtime.secrets import write_control_plane_env

    target = tmp_path / "demo"
    # Stand in for what `loopy auth github` writes before the scaffold runs.
    write_control_plane_env(target, {"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": "pem-here"})

    scaffold_project(target, "demo", repos=["me/app"])

    env = load_control_plane_env(target)
    assert env["GITHUB_APP_ID"] == "42"
    assert env["GITHUB_APP_PRIVATE_KEY"] == "pem-here"
    # The template's explanatory comments still land alongside the preserved creds.
    assert "Control-plane credentials" in (target / "loopy.env").read_text()


def test_scaffold_still_refuses_dir_with_unrelated_file(tmp_path):
    """The pre-existing-file tolerance is scoped to loopy's own auth artifacts — any other content
    (e.g. a stray loopy.env alongside real work) still refuses so we never clobber it."""
    from loopy_runtime.secrets import write_control_plane_env

    target = tmp_path / "demo"
    write_control_plane_env(target, {"GITHUB_APP_ID": "42"})
    (target / "keep.txt").write_text("mine")
    with pytest.raises(FileExistsError):
        scaffold_project(target, "demo")


def test_init_asks_public_url_then_auth_then_repos(tmp_path, monkeypatch):
    """The wizard asks for the hosted URL first — the GitHub auth offer that follows bakes the
    webhook URL into the App at creation — then authenticates (receiving that URL), then asks
    which repo(s) to work on. Verified by call order and the URL pass-through."""
    calls: list[str] = []
    auth_received: list[str | None] = []

    monkeypatch.setattr(
        loopy_cli,
        "_prompt_for_public_url",
        lambda: (calls.append("url"), "https://loopy.example.com")[1],
    )

    def fake_auth(target, public_url=None):
        calls.append("auth")
        auth_received.append(public_url)

    monkeypatch.setattr(loopy_cli, "_offer_github_auth", fake_auth)
    monkeypatch.setattr(loopy_cli, "_prompt_for_repos", lambda: (calls.append("repos"), [])[1])
    # Keep the rest of the interactive wizard quiet.
    monkeypatch.setattr(loopy_cli, "_offer_ambient_anthropic_key", lambda target: None)
    monkeypatch.setattr(loopy_cli, "_offer_ambient_daytona_creds", lambda target: None)
    monkeypatch.setattr(loopy_cli, "_offer_redis_bus", lambda target: None)
    monkeypatch.setattr(loopy_cli, "_note_orchestrator_mode", lambda: None)

    class _Tty:  # force the interactive branch (CliRunner's stdin reports not-a-tty)
        def isatty(self):
            return True

    monkeypatch.setattr(loopy_cli.sys, "stdin", _Tty())

    loopy_cli.init("demo", directory=tmp_path, non_interactive=False)

    assert calls == ["url", "auth", "repos"]
    assert auth_received == ["https://loopy.example.com"]  # the wizard's answer reaches auth
    # ...and the same URL lands in the scaffolded registry.
    registry = (tmp_path / "demo" / "registry.yml").read_text()
    assert "public_url: https://loopy.example.com" in registry


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


def test_ambient_daytona_offer_writes_creds_when_confirmed(tmp_path, monkeypatch):
    """A real DAYTONA_API_KEY (and URL) in the environment is written into loopy.env on confirm."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])
    assert "DAYTONA_API_KEY" not in load_control_plane_env(target)  # commented placeholder only

    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_realkey0123456789")
    monkeypatch.setenv("DAYTONA_API_URL", "https://daytona.example.com")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    loopy_cli._offer_ambient_daytona_creds(target)

    env = load_control_plane_env(target)
    assert env["DAYTONA_API_KEY"] == "dtn_realkey0123456789"
    assert env["DAYTONA_API_URL"] == "https://daytona.example.com"


def test_ambient_daytona_offer_carries_key_without_url(tmp_path, monkeypatch):
    """The URL is optional — a key with no DAYTONA_API_URL still lands the key alone."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_realkey0123456789")
    monkeypatch.delenv("DAYTONA_API_URL", raising=False)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    loopy_cli._offer_ambient_daytona_creds(target)

    env = load_control_plane_env(target)
    assert env["DAYTONA_API_KEY"] == "dtn_realkey0123456789"
    assert "DAYTONA_API_URL" not in env


def test_ambient_daytona_offer_respects_decline(tmp_path, monkeypatch):
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_realkey0123456789")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    loopy_cli._offer_ambient_daytona_creds(target)

    assert "DAYTONA_API_KEY" not in load_control_plane_env(target)


def test_ambient_daytona_offer_noop_without_env_key(tmp_path, monkeypatch):
    """No key in the environment ⇒ no prompt, no change (and crucially, no crash)."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)

    def _boom(*a, **k):  # the prompt must never be reached
        raise AssertionError("should not prompt when no ambient Daytona key is present")

    monkeypatch.setattr("typer.confirm", _boom)
    loopy_cli._offer_ambient_daytona_creds(target)

    assert "DAYTONA_API_KEY" not in load_control_plane_env(target)


def test_ambient_daytona_offer_does_not_clobber_existing(tmp_path, monkeypatch):
    """An already-configured key in loopy.env is left untouched — no prompt, no overwrite."""
    from loopy_runtime.secrets import write_control_plane_env

    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])
    write_control_plane_env(target, {"DAYTONA_API_KEY": "dtn_already_set"})

    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_ambient_different")

    def _boom(*a, **k):  # must not prompt when a key is already configured
        raise AssertionError("should not prompt when loopy.env already has a key")

    monkeypatch.setattr("typer.confirm", _boom)
    loopy_cli._offer_ambient_daytona_creds(target)

    assert load_control_plane_env(target)["DAYTONA_API_KEY"] == "dtn_already_set"


def test_public_url_prompt_returns_normalized_url(monkeypatch):
    """A hosted URL given at init is normalized (https assumed for a bare host, trailing slash
    stripped) so registry.yml carries the canonical base URL."""
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "loopy.example.com/")
    assert loopy_cli._prompt_for_public_url() == "https://loopy.example.com"


def test_public_url_prompt_blank_means_not_hosted(monkeypatch):
    """Blank is a first-class answer — the scaffold keeps public_url as a commented stub."""
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "")
    assert loopy_cli._prompt_for_public_url() is None


def test_public_url_prompt_drops_unusable_answer(monkeypatch):
    """An unusable answer is dropped with a pointer, never baked into the registry — a bad URL
    there would surface as silent webhook non-delivery, far from this prompt."""
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "ftp://loopy internal")
    assert loopy_cli._prompt_for_public_url() is None


def test_scaffold_writes_public_url_into_registry(tmp_path):
    """A public URL given at init lands as registry.yml's top-level `public_url` — in both
    starters — and the result still compiles green with the field parsed."""
    for label, repos in (("coding", ["me/app"]), ("orch", None)):
        target = tmp_path / label
        scaffold_project(target, "demo", repos=repos, public_url="https://loopy.example.com")

        assert "public_url: https://loopy.example.com" in (target / "registry.yml").read_text()
        result = compile_project(target)
        assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
        assert result.project.registry.public_url == "https://loopy.example.com"


def test_scaffold_without_public_url_leaves_commented_stub(tmp_path):
    """No URL at init still surfaces the field: a commented `# public_url:` stub the user can
    fill in by hand any time, with nothing active."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo", repos=["me/app"])

    assert "# public_url:" in (target / "registry.yml").read_text()
    result = compile_project(target)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    assert result.project.registry.public_url is None


def test_redis_offer_writes_url_when_confirmed(tmp_path, monkeypatch):
    """Opting into Redis records the connection string in loopy.env — and writes no loopy.yaml."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "redis://localhost:6379")
    loopy_cli._offer_redis_bus(target)

    assert load_control_plane_env(target)["REDIS_URL"] == "redis://localhost:6379"
    # No config file is introduced — the bus is selected at launch with `loopy run --bus redis`.
    assert not (target / "loopy.yaml").exists()


def test_redis_offer_writes_custom_url(tmp_path, monkeypatch):
    """A non-default connection string is written through verbatim."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "redis://cache.internal:6380/2")
    loopy_cli._offer_redis_bus(target)

    assert load_control_plane_env(target)["REDIS_URL"] == "redis://cache.internal:6380/2"


def test_redis_offer_respects_decline(tmp_path, monkeypatch):
    """Declining leaves the in-process default — no REDIS_URL, no loopy.yaml, no URL prompt."""
    target = tmp_path / "demo"
    scaffold_project(target, "demo")

    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)

    def _boom(*a, **k):  # the connection-string prompt must never be reached on decline
        raise AssertionError("should not prompt for a URL when Redis is declined")

    monkeypatch.setattr("typer.prompt", _boom)
    loopy_cli._offer_redis_bus(target)

    assert "REDIS_URL" not in load_control_plane_env(target)
    assert not (target / "loopy.yaml").exists()


def test_redis_offer_does_not_clobber_existing(tmp_path, monkeypatch):
    """An already-configured REDIS_URL in loopy.env is left untouched — no prompt, no overwrite."""
    from loopy_runtime.secrets import write_control_plane_env

    target = tmp_path / "demo"
    scaffold_project(target, "demo")
    write_control_plane_env(target, {"REDIS_URL": "redis://existing:6379"})

    def _boom(*a, **k):  # must not prompt when a URL is already configured
        raise AssertionError("should not prompt when loopy.env already has REDIS_URL")

    monkeypatch.setattr("typer.confirm", _boom)
    loopy_cli._offer_redis_bus(target)

    assert load_control_plane_env(target)["REDIS_URL"] == "redis://existing:6379"


def test_init_non_interactive_writes_placeholder_scaffold(tmp_path, monkeypatch):
    """`--non-interactive` skips every prompt and leaves the verbatim placeholder scaffold."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-realkey0123456789")  # would be offered if asked

    result = runner.invoke(app, ["init", "demo", "--dir", str(tmp_path), "--non-interactive"])

    assert result.exit_code == 0, result.output
    target = tmp_path / "demo"
    assert _key_line(target) == f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"
    # The doctor-backed summary replaces the old static checklist. With no repo (the
    # non-interactive default), two gaps remain: the placeholder model key and the missing
    # DAYTONA_API_KEY the scaffold's `provider: daytona` sandbox needs.
    assert "2 things left" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "DAYTONA_API_KEY" in result.output
    # No repo ⇒ the non-coding orchestrator starter, never an unpushable placeholder repo.
    registry = (target / "registry.yml").read_text()
    assert "summarize" in registry
    assert "octocat" not in registry.lower()


def test_init_non_interactive_requires_name(tmp_path):
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--non-interactive"])
    assert result.exit_code == 1
    assert "name is required" in result.output
