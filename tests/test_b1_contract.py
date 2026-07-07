"""B1: the runtime contract loads the real manifest and exposes the §3.4 seams."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopy_runtime import contract
from loopy_runtime.manifest_model import Manifest, load_manifest
from loopy_runtime.providers import (
    REQUIRED_MODEL_KEY,
    canonical_model,
    required_model_key,
    validate_model,
)

GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"


def test_loads_incidents_manifest_into_typed_model():
    m = load_manifest(GOLDEN)
    assert isinstance(m, Manifest)
    assert m.schema_version == "2"
    # workflows + steps typed
    assert "triage" in m.workflows
    assert m.workflows["triage"].entry == "investigate"
    # sandbox env_file carried through (the addendum)
    assert m.registry.sandboxes["default"].env_file == ["secrets/base.env"]
    # agent model + harness present as flat keys (both mandatory; the harness drives
    # the provider-key requirement)
    assert m.registry.agents["Investigator"].model == "claude-sonnet-4-6"
    assert m.registry.agents["Investigator"].harness == "claude-code"
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


def test_trigger_spec_filters_default_empty_and_match_exactly():
    from loopy_runtime.manifest_model import TriggerSpec

    # A pre-filters trigger dict (no `filters` key) loads as unfiltered — matches everything.
    legacy = TriggerSpec.model_validate({"kind": "event", "event": "Incident"})
    assert legacy.filters == {}
    assert legacy.matches({"anything": "at all"})

    # The golden (unfiltered) manifest carries the same semantics explicitly.
    m = load_manifest(GOLDEN)
    entry = m.workflows["triage"].steps["investigate"]
    assert entry.trigger.filters == {}

    spec = TriggerSpec(
        kind="event",
        event="PROpened",
        filters={"repo": "octocat/Hello-World", "base": "main"},
    )
    assert spec.matches({"repo": "octocat/Hello-World", "base": "main", "number": 7})
    assert not spec.matches({"repo": "octocat/Hello-World", "base": "dev"})  # AND, not OR
    assert not spec.matches({"repo": "someone/else", "base": "main"})
    assert not spec.matches({})  # a missing field never matches


def test_provider_registry_resolves_v1_runtimes():
    assert required_model_key("claude-code") == "ANTHROPIC_API_KEY"
    assert required_model_key("codex") == "OPENAI_API_KEY"  # codex harness is wired now
    # opencode's key derives from the model's provider, so the model is required.
    assert required_model_key("opencode", "anthropic/claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
    assert required_model_key("opencode", "openai/gpt-5.5") == "OPENAI_API_KEY"
    # Bare ids work too — sugar expands them before the key lookup.
    assert required_model_key("opencode", "claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
    assert required_model_key("opencode", "gpt-5.5") == "OPENAI_API_KEY"
    with pytest.raises(ValueError):
        required_model_key("opencode")  # no model -> no provider to derive the key from
    # REQUIRED_MODEL_KEY covers only the statically-keyed runtimes.
    assert set(REQUIRED_MODEL_KEY) == {"claude-code", "codex"}
    with pytest.raises(ValueError):
        required_model_key("unknown-runtime")


def test_model_eligibility_per_harness():
    # Each harness only drives its provider's models.
    validate_model("claude-code", "claude-opus-4-8")
    validate_model("codex", "gpt-5-codex")
    validate_model("opencode", "anthropic/claude-sonnet-4-6")
    validate_model("opencode", "openai/gpt-5.5")
    # opencode also takes the bare ids the other runtimes use (sugar expands them).
    validate_model("opencode", "claude-opus-4-8")
    validate_model("opencode", "gpt-5-codex")
    # A missing model is always rejected — every agent names one; there is no
    # fallback to a CLI's own default model.
    with pytest.raises(ValueError, match="must name a model"):
        validate_model("codex", None)
    with pytest.raises(ValueError, match="must name a model"):
        validate_model("opencode", None)
    # Cross-provider pairings are rejected.
    with pytest.raises(ValueError):
        validate_model("claude-code", "gpt-5-codex")
    with pytest.raises(ValueError):
        validate_model("codex", "claude-opus-4-8")
    with pytest.raises(ValueError):
        validate_model("codex", "o3")  # o-series models are not supported
    with pytest.raises(ValueError):
        validate_model("opencode", "gemini-2.5-pro")  # no recognized provider serves it


def test_canonical_model_expands_opencode_sugar_only():
    # A bare id gains its opencode provider namespace...
    assert canonical_model("opencode", "claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert canonical_model("opencode", "gpt-5.5") == "openai/gpt-5.5"
    # ...an explicit provider/model passes through untouched...
    assert canonical_model("opencode", "anthropic/claude-sonnet-4-6") == (
        "anthropic/claude-sonnet-4-6"
    )
    # ...and single-provider runtimes never rewrite their models.
    assert canonical_model("claude-code", "claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert canonical_model("codex", "gpt-5-codex") == "gpt-5-codex"


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
