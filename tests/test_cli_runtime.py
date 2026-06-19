"""CLI runtime wiring (#7): the shared `build_runtime` helper + GitHub App token
injection on both `run` and `trigger`.

`run` and `trigger` used to construct the runtime independently, and only `run` wired
the token provider — so the documented one-shot test path (`trigger`) couldn't exercise
a repo-touching workflow. Both now go through `build_runtime`, and both mint tokens via
`_make_token_provider` (opt out with `--no-tokens`). No live network: a configured App is
simulated with a control-plane `loopy.env`; the provider is built, not exercised.
"""

from __future__ import annotations

from typer.testing import CliRunner

from loopy_cli import _make_token_provider, app, build_runtime
from loopy_runtime.bus.inproc import InProcessEventBus
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


# --- build_runtime ------------------------------------------------------------


def test_build_runtime_wires_tokens_and_bus(tmp_path):
    bus = InProcessEventBus()
    sentinel = object()
    rt = build_runtime(_manifest(), root=tmp_path, sandbox="local", bus=bus, tokens=sentinel)
    assert isinstance(rt, InMemoryRuntime)
    assert rt.tokens is sentinel  # the seam that injects creds into the sandbox
    assert rt.bus is bus
    assert isinstance(rt.secrets, EnvFileSecretsResolver)


def test_build_runtime_passes_state_through_when_given(tmp_path):
    state = InMemoryStateStore()
    rt = build_runtime(
        _manifest(), root=tmp_path, sandbox="local", bus=InProcessEventBus(), state=state
    )
    assert rt.state is state  # a networked bus shares the runtime's StateStore
    assert rt.tokens is None  # default: no injection unless wired


# --- trigger CLI surface ------------------------------------------------------


def test_trigger_exposes_no_tokens_opt_out():
    result = runner.invoke(app, ["trigger", "--help"])
    assert result.exit_code == 0
    assert "--no-tokens" in result.stdout


def _manifest():
    from loopy_runtime.manifest_model import Manifest

    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Go": {"fields": {}}}},
            "workflows": {"wf": {"entry": "run", "steps": {}}},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )
