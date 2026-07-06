"""CLI runtime wiring (#7): the shared `build_runtime` helper + GitHub App token
injection on both `run` and `trigger`.

`run` and `trigger` used to construct the runtime independently, and only `run` wired
the token provider — so the documented one-shot test path (`trigger`) couldn't exercise
a repo-touching workflow. Both now go through `build_runtime`, and both mint tokens via
`_make_token_provider` (opt out with `--no-tokens`). No live network: a configured App is
simulated with a control-plane `loopy.env`; the provider is built, not exercised.
"""

from __future__ import annotations

import os
import socket

import pytest
import typer
from typer.testing import CliRunner

from loopy_cli import (
    _make_token_provider,
    _resolve_dashboard_port,
    _run_record,
    app,
    build_runtime,
)
from loopy_cli.scaffold import scaffold_project
from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import RunStatus, StepOutput
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.scm.token_provider import GitHubAppTokenProvider
from loopy_runtime.secrets import EnvFileSecretsResolver
from loopy_runtime.state.inmemory import InMemoryStateStore

runner = CliRunner()


def _configure_app(root) -> None:
    """Write a control-plane `loopy.env` (+ PEM file) as `loopy auth github` would."""
    (root / "key.pem").write_text("PEM")
    (root / "loopy.env").write_text(
        "GITHUB_APP_ID=123\nGITHUB_APP_PRIVATE_KEY_FILE=key.pem\n"
    )


# --- _make_token_provider ----------------------------------------------------


def test_token_provider_disabled_returns_none(tmp_path):
    _configure_app(tmp_path)  # App is configured, but...
    assert _make_token_provider(tmp_path, enabled=False, announce=False) is None  # --no-tokens


def test_token_provider_none_without_app(tmp_path, monkeypatch):
    # No loopy.env and no App env vars → no injection (unchanged offline behavior).
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    assert _make_token_provider(tmp_path, enabled=True, announce=False) is None


def test_token_provider_built_from_control_plane_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    _configure_app(tmp_path)
    provider = _make_token_provider(tmp_path, enabled=True, announce=False)
    assert isinstance(provider, GitHubAppTokenProvider)


def test_token_provider_raises_when_configured_but_key_unloadable(tmp_path, monkeypatch):
    # GITHUB_APP_ID set but the key won't resolve → fail loudly with the real reason,
    # not a silent None that later reads as a misleading "no GitHub auth is configured".
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_FILE", raising=False)
    (tmp_path / "loopy.env").write_text(
        "GITHUB_APP_ID=123\nGITHUB_APP_PRIVATE_KEY_FILE=missing.pem\n"
    )
    with pytest.raises(RuntimeError, match="private key could not be loaded"):
        _make_token_provider(tmp_path, enabled=True, announce=False)


# --- build_runtime ------------------------------------------------------------


def test_build_runtime_wires_tokens_and_bus(tmp_path):
    bus = InProcessEventBus()
    sentinel = object()
    rt = build_runtime(_manifest(), root=tmp_path, bus=bus, tokens=sentinel)
    assert isinstance(rt, InMemoryRuntime)
    assert rt.tokens is sentinel  # the seam that injects creds into the sandbox
    assert rt.bus is bus
    assert isinstance(rt.secrets, EnvFileSecretsResolver)


def test_build_runtime_passes_state_through_when_given(tmp_path):
    state = InMemoryStateStore()
    rt = build_runtime(_manifest(), root=tmp_path, bus=InProcessEventBus(), state=state)
    assert rt.state is state  # a networked bus shares the runtime's StateStore
    assert rt.tokens is None  # default: no injection unless wired


def test_build_runtime_defaults_cascade_budget_to_none(tmp_path):
    rt = build_runtime(_manifest(), root=tmp_path, bus=InProcessEventBus())
    assert rt.cascade_budget_usd is None  # disabled unless registry limits.cascade_spend is set


def test_build_runtime_reads_cascade_budget_from_registry_limits(tmp_path):
    # The cap is a project-level control authored in the registry and carried by the manifest —
    # not a launch-time flag — so build_runtime derives it from registry.limits.cascade_spend.
    manifest = _manifest(limits={"cascade_spend": {"usd": 12.50}})
    rt = build_runtime(manifest, root=tmp_path, bus=InProcessEventBus())
    assert rt.cascade_budget_usd == 12.50


# --- trigger CLI surface ------------------------------------------------------


def test_trigger_exposes_no_tokens_opt_out():
    result = runner.invoke(app, ["trigger", "--help"])
    assert result.exit_code == 0
    assert "--no-tokens" in result.stdout


def test_trigger_exposes_json_flag():
    result = runner.invoke(app, ["trigger", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.stdout


def test_trigger_loads_control_plane_env(tmp_path, monkeypatch):
    # `trigger` must load loopy.env into the process env like `run` does — otherwise the
    # Daytona client (which reads os.environ) never sees DAYTONA_API_KEY and a `provider:
    # daytona` sandbox dies with "not set" even though the key is in loopy.env. Firing an
    # unsubscribed event exits before any sandbox is acquired, so this stays offline.
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    root = tmp_path / "demo"
    scaffold_project(root, "demo")
    (root / "loopy.env").write_text("DAYTONA_API_KEY=dtn-real\n")
    try:
        result = runner.invoke(
            app, ["trigger", str(root), "--event", "NopeNotReal", "--no-tokens"]
        )
        assert "loaded 1 control-plane var(s)" in result.output, result.output
        assert os.environ.get("DAYTONA_API_KEY") == "dtn-real"
    finally:
        os.environ.pop("DAYTONA_API_KEY", None)  # setdefault leaks into the in-process env


# --- run record (#9: surface step outputs) -----------------------------------


class _FakeRun:
    """The bits of InMemoryRuntime that `_run_record` reads."""

    def __init__(self, execution_log, emitted_log, failed_runs):
        self.execution_log = execution_log
        self.emitted_log = emitted_log
        self.failed_runs = failed_runs


def test_run_record_collects_steps_outputs_and_emits():
    rt = _FakeRun(["fix"], ["WorkItem"], [])
    outputs = {"fix": StepOutput({"pr_url": "https://pr/1"})}
    assert _run_record("r1", rt, outputs) == {
        "run": "r1",
        "steps": ["fix"],
        "emitted": ["WorkItem"],
        "outputs": {"fix": {"pr_url": "https://pr/1"}},
        "failed": [],
    }


def test_run_record_includes_failures():
    rt = _FakeRun(["fix"], [], [RunStatus(run_id="r1", state="failed", error="boom")])
    assert _run_record("r1", rt, {})["failed"] == [{"run_id": "r1", "error": "boom"}]


def _manifest(*, limits=None):
    from loopy_runtime.manifest_model import Manifest

    registry = {"sandboxes": {}, "agents": {}, "events": {"Go": {"fields": {}}}}
    if limits is not None:
        registry["limits"] = limits
    return Manifest.model_validate(
        {
            "schema_version": "2",
            "registry": registry,
            "workflows": {"wf": {"entry": "run", "steps": {}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


# --- dashboard port resolution (resilience: don't crash when the port is taken) ---


def _bind_busy_port():
    """Bind and listen on an OS-assigned port; return (socket, port). Caller closes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_resolve_dashboard_port_returns_preferred_when_free():
    # Grab a free port, release it, then confirm the resolver hands it straight back.
    probe, port = _bind_busy_port()
    probe.close()
    assert _resolve_dashboard_port("127.0.0.1", port) == port


def test_resolve_dashboard_port_skips_busy_port():
    sock, busy = _bind_busy_port()
    try:
        chosen = _resolve_dashboard_port("127.0.0.1", busy)
    finally:
        sock.close()
    assert chosen != busy
    # The fallback must itself be bindable.
    check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        check.bind(("127.0.0.1", chosen))
    finally:
        check.close()


def test_resolve_dashboard_port_exits_when_window_exhausted():
    # A single-slot window over a busy port has nowhere to fall back: clean exit, no traceback.
    sock, busy = _bind_busy_port()
    try:
        with pytest.raises(typer.Exit) as exc:
            _resolve_dashboard_port("127.0.0.1", busy, attempts=1)
    finally:
        sock.close()
    assert exc.value.exit_code == 1


# --- loopy help ---------------------------------------------------------------


def test_help_lists_all_commands():
    """`loopy help` prints the top-level overview, including every registered command."""
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0, result.output
    for command in ("init", "compile", "doctor", "run", "trigger", "help", "auth"):
        assert command in result.output


def test_help_for_a_command_shows_its_usage():
    """`loopy help run` renders that command's own help (its usage line + options)."""
    result = runner.invoke(app, ["help", "run"])
    assert result.exit_code == 0, result.output
    # CliRunner uses a default prog name, so match the command-specific tail, not the prefix.
    assert "run [OPTIONS]" in result.output
    assert "--bus" in result.output  # a flag unique to `run`


def test_help_walks_into_sub_apps():
    """`loopy help auth github` descends through the auth sub-app to the leaf command."""
    result = runner.invoke(app, ["help", "auth", "github"])
    assert result.exit_code == 0, result.output
    assert "auth github [OPTIONS]" in result.output


def test_help_unknown_command_errors():
    result = runner.invoke(app, ["help", "bogus"])
    assert result.exit_code == 1
    assert "unknown command 'bogus'" in result.output


def test_help_rejects_subcommand_of_a_leaf():
    result = runner.invoke(app, ["help", "run", "extra"])
    assert result.exit_code == 1
    assert "no subcommands" in result.output


def test_bare_loopy_outside_project_exits_zero():
    """A bare `loopy` (no subcommand) exits 0 — a request for orientation, not a usage error
    (Typer's default would be exit 2)."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output


def test_help_flag_lists_every_command():
    """`loopy --help` remains the complete reference: every command, project-scoped or not."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("init", "compile", "doctor", "run", "trigger", "help", "auth"):
        assert command in result.output


def _clear_project_env(monkeypatch) -> None:
    """Drop any inherited state that would change the next-steps ladder under test."""
    for key in ("GITHUB_APP_ID", "LOOPY_PUBLIC_URL", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def test_bare_loopy_in_project_prints_next_steps(tmp_path, monkeypatch):
    """Inside a project directory a bare `loopy` prints the guided next-steps ladder instead of
    the command overview — the onboarding path for "what do I do next here?"."""
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])
    _clear_project_env(monkeypatch)
    monkeypatch.chdir(root)

    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Suggested next steps" in result.output
    # No URL and no git auth yet: the first two rungs of the ladder.
    assert "loopy deploy bootstrap" in result.output
    assert "loopy auth github" in result.output
    # It's the ladder, not the overview — no Typer usage banner.
    assert "Usage: loopy" not in result.output


def test_bare_loopy_in_configured_project_suggests_webhooks(tmp_path, monkeypatch):
    """With an App and a public URL wired, the ladder's remaining rung is registering delivery."""
    root = tmp_path / "demo"
    scaffold_project(root, "demo", repos=["me/app"])
    _clear_project_env(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("LOOPY_PUBLIC_URL", "https://x.example")
    monkeypatch.chdir(root)

    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "loopy webhooks github" in result.output
    assert "loopy deploy bootstrap" not in result.output


def test_bare_loopy_outside_project_points_at_init(tmp_path, monkeypatch):
    """Outside a project (no registry.yml) a bare `loopy` leads with `loopy init` — the one
    command that applies with no project yet — and names docs/help, rather than dumping the
    full command overview whose entries all need a project. The complete list stays on
    `loopy --help`."""
    _clear_project_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Suggested next steps" not in result.output  # not the in-project ladder
    assert "loopy init" in result.output
    assert "loopy docs" in result.output
    assert "loopy help" in result.output
    # It's orientation, not the overview: no Typer usage banner, and none of the
    # project-scoped commands are listed here.
    assert "Usage: loopy" not in result.output
    for project_command in ("loopy compile", "loopy run", "loopy doctor", "loopy trigger"):
        assert project_command not in result.output
