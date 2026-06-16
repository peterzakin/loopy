"""Typed view of the compiled manifest (FRONTEND §9) — the backend's only input.

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


class HarnessSpec(_Model):
    runtime: str | None = None
    model: str | None = None


class SandboxSpec(_Model):
    provider: str | None = None
    image: dict = Field(default_factory=dict)
    network: list[str] = Field(default_factory=list)
    env_file: list[str] = Field(default_factory=list)


class AgentSpec(_Model):
    harness: HarnessSpec = Field(default_factory=HarnessSpec)
    sandbox: str | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class EventContract(_Model):
    fields: dict[str, dict] = Field(default_factory=dict)


class RegistrySpec(_Model):
    sandboxes: dict[str, SandboxSpec] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    events: dict[str, EventContract] = Field(default_factory=dict)


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
    module: str
    fn: str


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


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.model_validate(json.loads(Path(path).read_text()))
