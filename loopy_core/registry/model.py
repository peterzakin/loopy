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


class Repo(BaseModel):
    # A GitHub repo to clone into the sandbox workspace at acquire time. `url` is an
    # `owner/name` shorthand or a full https URL; the rest are optional checkout knobs.
    url: str
    ref: str | None = None  # branch/tag/SHA; the repo's default branch when omitted
    path: str | None = None  # subdir under the workdir; defaults to the repo name
    depth: int | None = 1  # shallow clone depth; None for full history


class Sandbox(BaseModel):
    name: str
    provider: str | None = None
    image: dict = Field(default_factory=dict)
    network: list[str] = Field(default_factory=list)
    # Path(s) to env file(s) supplying this sandbox's secrets. A *reference* only —
    # the compiler records it and never reads the file (values resolve at run time).
    env_file: list[str] = Field(default_factory=list)
    # GitHub repos cloned into the workspace at acquire time (FRONTEND §2). Auth rides
    # the credentials the runtime injects; egress must be allowed by `network`.
    repos: list[Repo] = Field(default_factory=list)
    span: Span


class Agent(BaseModel):
    name: str
    harness: Harness = Field(default_factory=Harness)
    sandbox: str | None = None
    skills: list[str] = Field(default_factory=list)
    span: Span


class Event(BaseModel):
    name: str
    # top-level key -> JSON Schema fragment (draft 2020-12)
    fields: dict[str, dict] = Field(default_factory=dict)
    span: Span


class Limits(BaseModel):
    # Project-level controls (FRONTEND §2). `cascade_spend` caps the cumulative USD a single
    # cascade may spend across every reachable step — including event loop-backs, which can span
    # workflows — so it's a project policy, not a per-workflow one.
    cascade_spend: dict | None = None  # e.g. {"usd": 50}


class Registry(BaseModel):
    sandboxes: dict[str, Sandbox] = Field(default_factory=dict)
    agents: dict[str, Agent] = Field(default_factory=dict)
    events: dict[str, Event] = Field(default_factory=dict)
    limits: Limits | None = None
