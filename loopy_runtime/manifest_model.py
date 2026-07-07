"""Typed view of the compiled manifest — the backend's only input.

`load_manifest(path)` parses `manifest.json` into these models. CLI-stamped
provenance fields (`compiled_at`/`loopy_version`) are tolerated but optional.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The manifest schema version this engine can read. Must equal the compiler's
# `loopy_core.compile.manifest.SCHEMA_VERSION` — they ship together in the same wheel, and
# `test_supported_schema_version_tracks_compiler` pins them equal so a bump can't land in one
# without the other. Extra keys are tolerated (see `_Model`), so only a *major* shape change
# (a field's type flipping, a key moving) warrants bumping this and gating on it.
SUPPORTED_SCHEMA_VERSION = "2"


class ManifestSchemaError(Exception):
    """A manifest was compiled for a schema version this engine can't read.

    Raised before Pydantic validation so a version skew surfaces as one clear line naming both
    versions and the remedy, instead of a wall of `model_type` errors from every field the new
    shape moved. The usual cause is an engine image older than the CLI that compiled the manifest
    (a re-deploy that reused a cached `loopy-engine:<version>` image, or a dev CLI that has run
    ahead of the last published engine release).
    """


class _Model(BaseModel):
    # Tolerate extra keys so manifest evolution doesn't break older readers.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())


class RepoSpec(_Model):
    url: str
    ref: str | None = None
    path: str | None = None
    depth: int | None = 1


class SandboxSpec(_Model):
    provider: str | None = None
    image: dict = Field(default_factory=dict)
    network: list[str] = Field(default_factory=list)
    env_file: list[str] = Field(default_factory=list)
    repos: list[RepoSpec] = Field(default_factory=list)


class AgentSpec(_Model):
    # Flat and mandatory (schema v2): `model` names the model the agent runs on,
    # `harness` the runner that drives it (`claude-code` | `codex` | `opencode`).
    # Neither is ever inferred from the other; the compiler enforces presence (E507)
    # and each harness enforces the model's eligibility for its runtime.
    model: str
    harness: str
    sandbox: str | None = None
    skills: list[str] = Field(default_factory=list)


class EventContract(_Model):
    fields: dict[str, dict] = Field(default_factory=dict)


class WorkflowLimitSpec(_Model):
    spend: dict | None = None  # e.g. {"usd": 10}


class LimitsSpec(_Model):
    cascade_spend: dict | None = None  # e.g. {"usd": 50}
    workflows: dict[str, WorkflowLimitSpec] = Field(default_factory=dict)


class RegistrySpec(_Model):
    sandboxes: dict[str, SandboxSpec] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    events: dict[str, EventContract] = Field(default_factory=dict)
    limits: LimitsSpec | None = None


class TriggerSpec(_Model):
    kind: str  # "event" | "cron"
    event: str | None = None
    # kind == "event": field -> required value (from `on: Event(field="value")`). All must
    # match (AND) for a published event to start the workflow; empty means unfiltered.
    filters: dict[str, str] = Field(default_factory=dict)
    expr: str | None = None
    tz: str | None = None

    def matches(self, fields: dict) -> bool:
        """Whether an event's validated fields satisfy every filter. Filters are only ever
        compiled against string-typed contract fields (E114), so exact string comparison is
        the whole semantic."""
        return all(fields.get(key) == want for key, want in self.filters.items())


class BudgetSpec(_Model):
    wall_clock: int | None = None
    spend: dict | None = None
    window: int | None = None
    latency: int | None = None


class RefSpec(_Model):
    producer: str
    field: str
    raw: str


class StepSpec(_Model):
    id: str
    trigger: TriggerSpec | None = None
    after: list[str] = Field(default_factory=list)
    agent: str | None = None
    output: dict[str, dict] = Field(default_factory=dict)
    emits: list[str] = Field(default_factory=list)
    budget: BudgetSpec | None = None
    body: str = ""
    refs: list[RefSpec] = Field(default_factory=list)


class WorkflowSpec(_Model):
    entry: str | None = None
    steps: dict[str, StepSpec] = Field(default_factory=dict)


class SensorTriggerSpec(_Model):
    kind: str  # "webhook" | "poll"
    path: str | None = None
    interval: str | None = None


class SensorSpec(_Model):
    name: str
    trigger: SensorTriggerSpec
    emits: str
    # "module" (user-authored; import module.fn) or "builtin" (platform-shipped; resolve a
    # mapper from the runtime registry by `emits`). Built-ins leave module/fn unset.
    source: str = "module"
    provider: str | None = None
    module: str | None = None
    fn: str | None = None


class EventLineageSpec(_Model):
    producers: list[str] = Field(default_factory=list)
    consumers: list[str] = Field(default_factory=list)


class LineageSpec(_Model):
    events: dict[str, EventLineageSpec] = Field(default_factory=dict)


class Manifest(_Model):
    schema_version: str
    registry: RegistrySpec = Field(default_factory=RegistrySpec)
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)
    sensors: list[SensorSpec] = Field(default_factory=list)
    lineage: LineageSpec = Field(default_factory=LineageSpec)
    # CLI-stamped, outside the hashed core.
    compiled_at: str | None = None
    loopy_version: str | None = None

    def workflow_for_event(self, event_name: str) -> tuple[str, StepSpec] | None:
        """The (workflow_name, entry_step) whose on: matches `event_name`, if any."""
        for wf_name, wf in self.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "event":
                if entry.trigger.event == event_name:
                    return wf_name, entry
        return None

    def workflow_for_cron(self, trigger_id: str) -> tuple[str, StepSpec] | None:
        """The (workflow_name, entry_step) whose cron entry has id `trigger_id`, if any. The
        cron trigger is keyed by its entry step id (one cron entry per workflow)."""
        for wf_name, wf in self.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "cron" and entry.id == trigger_id:
                return wf_name, entry
        return None

    def cron_entries(self) -> list[tuple[str, StepSpec]]:
        """Every workflow's cron entry step, as (workflow_name, entry_step). For wiring the
        scheduler at startup."""
        out: list[tuple[str, StepSpec]] = []
        for wf_name, wf in self.workflows.items():
            entry = wf.steps.get(wf.entry) if wf.entry else None
            if entry and entry.trigger and entry.trigger.kind == "cron":
                out.append((wf_name, entry))
        return out


def load_manifest(path: str | Path) -> Manifest:
    doc = json.loads(Path(path).read_text())
    # Gate on schema_version *before* Pydantic. A too-new manifest fails validation field by field
    # (every moved key becomes its own error), which reads as a bug in the manifest rather than the
    # real cause — a stale engine. Check the contract version first and say so plainly.
    version = doc.get("schema_version")
    if version is not None and str(version) != SUPPORTED_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"manifest schema_version {version!r} is not readable by this engine "
            f"(it supports {SUPPORTED_SCHEMA_VERSION!r}). The engine is out of sync with the CLI "
            f"that compiled this manifest — its image is older than the manifest's schema. "
            f"Rebuild/redeploy the engine so its version matches the CLI (for an unreleased build, "
            f"`loopy deploy bootstrap --engine-source <checkout>`)."
        )
    try:
        return Manifest.model_validate(doc)
    except ValidationError as exc:
        # A validation failure on a version we *do* claim to support is still most often a skew
        # (e.g. the version wasn't bumped when the shape changed). Point at that rather than let a
        # raw Pydantic dump be the whole story.
        raise ManifestSchemaError(
            f"manifest at {path} did not match this engine's schema (declared schema_version "
            f"{version!r}, engine supports {SUPPORTED_SCHEMA_VERSION!r}). This usually means the "
            f"engine image is out of sync with the CLI that compiled the manifest; "
            f"rebuild/redeploy the engine to match.\n\n{exc}"
        ) from exc
