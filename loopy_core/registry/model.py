"""Registry IR (FRONTEND §2). Field types are plain JSON Schema fragments (dicts);
the type DSL desugars to them in `registry/types.py`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from loopy_core.span import Span


class Harness(BaseModel):
    # `model` would otherwise collide with pydantic's protected `model_` namespace.
    model_config = ConfigDict(protected_namespaces=())

    runtime: str | None = None
    model: str | None = None


class Sandbox(BaseModel):
    name: str
    provider: str | None = None
    image: dict = Field(default_factory=dict)
    network: list[str] = Field(default_factory=list)
    # Path(s) to env file(s) supplying this sandbox's secrets. A *reference* only —
    # the compiler records it and never reads the file (values resolve at run time).
    env_file: list[str] = Field(default_factory=list)
    span: Span


class Agent(BaseModel):
    name: str
    harness: Harness = Field(default_factory=Harness)
    sandbox: str | None = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    span: Span


class Event(BaseModel):
    name: str
    # top-level key -> JSON Schema fragment (draft 2020-12)
    fields: dict[str, dict] = Field(default_factory=dict)
    span: Span


class Registry(BaseModel):
    sandboxes: dict[str, Sandbox] = Field(default_factory=dict)
    agents: dict[str, Agent] = Field(default_factory=dict)
    events: dict[str, Event] = Field(default_factory=dict)
