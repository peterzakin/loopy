"""Workflow IR (FRONTEND §2): triggers, steps, refs, and the per-workflow DAG."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from loopy_core.span import Span


class Trigger(BaseModel):
    """A step's `on:` — exactly one event, or a cron tick. Never a union."""

    kind: Literal["event", "cron"]
    event: str | None = None  # kind == "event"
    expr: str | None = None  # kind == "cron"
    tz: str | None = None  # kind == "cron", optional
    span: Span | None = None


class Budget(BaseModel):
    wall_clock: int | None = None  # minutes
    spend: dict | None = None  # e.g. {"usd": 4}
    window: int | None = None  # days
    latency: int | None = None  # days


class Ref(BaseModel):
    """A `{{ producer.field }}` reference extracted from a step body."""

    producer: str  # "event" | <step name>
    field: str
    raw: str  # the literal "{{ ... }}" text
    span: Span


class Step(BaseModel):
    id: str  # "<workflow>/<step>"
    workflow: str
    name: str
    trigger: Trigger | None = None  # set iff entry step
    after: list[str] = Field(default_factory=list)  # local step names
    agent: str | None = None
    output: dict[str, dict] = Field(default_factory=dict)  # key -> JSON Schema fragment
    emits: list[str] = Field(default_factory=list)
    budget: Budget | None = None
    body: str = ""
    refs: list[Ref] = Field(default_factory=list)
    span: Span


class Workflow(BaseModel):
    # `dag` holds a networkx.DiGraph, which pydantic cannot validate/serialize.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    entry: str | None = None  # the one `on:` step
    steps: dict[str, Step] = Field(default_factory=dict)
    dag: Any = Field(default=None, exclude=True, repr=False)
