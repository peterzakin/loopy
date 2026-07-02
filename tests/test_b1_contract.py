"""B1: the runtime contract loads the real manifest and exposes the §3.4 seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopy_runtime import contract
from loopy_runtime.manifest_model import Manifest, load_manifest
from loopy_runtime.providers import (
    REQUIRED_MODEL_KEY,
    required_model_key,
    validate_model,
)

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def test_loads_incidents_manifest_into_typed_model():
    m = load_manifest(GOLDEN)
    assert isinstance(m, Manifest)
    assert m.schema_version == "1"
    # workflows + steps typed
    assert "triage" in m.workflows
    assert m.workflows["triage"].entry == "investigate"
    # sandbox env_file carried through (the addendum)
    assert m.registry.sandboxes["default"].env_file == ["secrets/base.env"]
    # agent harness runtime present (drives the provider-key requirement)
    assert m.registry.agents["Investigator"].harness.runtime == "claude-code"
    # sensors typed
    assert {s.name for s in m.sensors} == {"sentry_issues", "metric_watch"}


def test_workflow_for_event_resolves_entry():
    m = load_manifest(GOLDEN)
    match = m.workflow_for_event("Incident")
    assert match is not None
    wf_name, entry = match
    assert wf_name == "triage"
    assert entry.id == "triage/investigate"
    assert m.workflow_for_event("NotARegisteredEvent") is None


def test_provider_registry_resolves_v1_runtimes():
    assert required_model_key("claude-code") == "ANTHROPIC_API_KEY"
    assert required_model_key("codex") == "OPENAI_API_KEY"  # codex harness is wired now
    # opencode's key derives from the model's provider prefix, so the model is required.
    assert required_model_key("opencode", "anthropic/claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
    assert required_model_key("opencode", "openai/gpt-5.5") == "OPENAI_API_KEY"
    with pytest.raises(ValueError):
        required_model_key("opencode")  # no model -> no prefix to derive the key from
    # REQUIRED_MODEL_KEY covers only the statically-keyed runtimes.
    assert set(REQUIRED_MODEL_KEY) == {"claude-code", "codex"}
    with pytest.raises(ValueError):
        required_model_key("unknown-runtime")


def test_model_eligibility_per_harness():
    # Each harness only drives its provider's models.
    validate_model("claude-code", "claude-opus-4-8")
    validate_model("codex", "gpt-5-codex")
    validate_model("codex", "o3")
    validate_model("opencode", "anthropic/claude-sonnet-4-6")
    validate_model("opencode", "openai/gpt-5.5")
    # A None model is allowed (falls back to the runtime default).
    validate_model("codex", None)
    # ... except for opencode, whose auth key derives from the model's provider prefix.
    with pytest.raises(ValueError, match="requires an explicit model"):
        validate_model("opencode", None)
    # Cross-provider pairings are rejected.
    with pytest.raises(ValueError):
        validate_model("claude-code", "gpt-5-codex")
    with pytest.raises(ValueError):
        validate_model("codex", "claude-opus-4-8")
    with pytest.raises(ValueError):
        validate_model("opencode", "claude-opus-4-8")  # bare model id, no provider prefix


def test_protocols_are_runtime_checkable():
    for name in (
        "Runtime",
        "StateStore",
        "AgentHarness",
        "SandboxProvider",
        "Sandbox",
        "EventBus",
        "EventReceiver",
        "SensorRunner",
        "RetryPolicy",
        "SecretsResolver",
    ):
        proto = getattr(contract, name)
        # runtime_checkable protocols accept isinstance() checks without raising.
        assert isinstance(object(), proto) is False
