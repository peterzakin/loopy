"""Typed view of the compiled manifest — the backend's only input.

`load_manifest(path)` parses `manifest.json` into these models. CLI-stamped
provenance fields (`compiled_at`/`loopy_version`) are tolerated but optional.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


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
    # Canonical env-var names the engine forwards from its own environment into this sandbox
    # (the production path). Compiler-validated (LOOPY-E216); resolved by the secrets layer
    # from the sandbox's `<PREFIX>_<KEY>` namespace, prefix stripped on injection.
    env: list[str] = Field(default_factory=list)
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
    expr: str | None = None
    tz: str | None = None


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
    return Manifest.model_validate(json.loads(Path(path).read_text()))
