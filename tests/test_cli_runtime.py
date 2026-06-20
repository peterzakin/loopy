"""CLI runtime wiring (#7): the shared `build_runtime` helper + GitHub App token
injection on both `run` and `trigger`.

`run` and `trigger` used to construct the runtime independently, and only `run` wired
the token provider — so the documented one-shot test path (`trigger`) couldn't exercise
a repo-touching workflow. Both now go through `build_runtime`, and both mint tokens via
`_make_token_provider` (opt out with `--no-tokens`). No live network: a configured App is
simulated with a control-plane `loopy.env`; the provider is built, not exercised.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from loopy_cli import _make_token_provider, _run_record, app, build_runtime
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


# --- run record (#9: surface step outputs) -----------------------------------


class _FakeRun:
    """The bits of InMemoryRuntime that `_run_record` reads."""

    def __init__(self, execution_log, emitted_log, failed_runs, verifications=None):
        self.execution_log = execution_log
        self.emitted_log = emitted_log
        self.failed_runs = failed_runs
        if verifications is not None:
            self.verifications = verifications


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


def test_run_record_omits_unverified_when_all_confirmed():
    from loopy_runtime.scm.verify import SCMVerification

    rt = _FakeRun(["fix"], [], [], verifications=[SCMVerification("pr_url", "u", "confirmed")])
    assert "unverified" not in _run_record("r1", rt, {})


def test_run_record_surfaces_unconfirmed_scm_outputs():
    from loopy_runtime.scm.verify import SCMVerification

    rt = _FakeRun(
        ["fix"],
        [],
        [],
        verifications=[
            SCMVerification("pr_url", "https://github.com/o/r/pull/9", "confirmed"),
            SCMVerification("pr_url", "https://github.com/o/r/pull/99", "not_found", "no PR"),
        ],
    )
    record = _run_record("r1", rt, {})
    assert record["unverified"] == [
        {
            "field": "pr_url",
            "url": "https://github.com/o/r/pull/99",
            "status": "not_found",
            "detail": "no PR",
        }
    ]


def _manifest(*, limits=None):
    from loopy_runtime.manifest_model import Manifest

    registry = {"sandboxes": {}, "agents": {}, "events": {"Go": {"fields": {}}}}
    if limits is not None:
        registry["limits"] = limits
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": registry,
            "workflows": {"wf": {"entry": "run", "steps": {}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
