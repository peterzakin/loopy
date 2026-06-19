"""P1 — parse registry.yml into a typed Registry with defaults applied (FRONTEND §2).

Parses with ruamel.yaml so keys retain line numbers for `file:line` spans, applies
`defaults.agent` inheritance (deep-merge `harness`, replace `tools`/`skills` lists,
scalar override for `sandbox`), desugars event field types, and runs X1 naming.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Agent, Event, Harness, Registry, Sandbox
from loopy_core.registry.types import desugar
from loopy_core.span import Span, span_at

_yaml = YAML()  # round-trip mode preserves line/col info


def _line_of(node: object, key: object) -> int:
    """1-based line of `key` within a ruamel CommentedMap, or 0 if unavailable."""
    try:
        return node.lc.data[key][0] + 1  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError):
        return 0


def _is_capitalized(name: str) -> bool:
    return bool(name[:1].isupper())


def _as_str_list(value: object) -> list[str]:
    """Normalize a scalar-or-list YAML value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def load_registry(inv: Inventory, diags: DiagnosticCollector) -> Registry:
    if inv.registry_path is None:
        diags.error(
            codes.E001,
            "registry.yml not found at the project root",
            span=span_at("registry.yml"),
        )
        return Registry()

    try:
        file = str(inv.registry_path.relative_to(inv.root))
    except ValueError:
        file = inv.registry_path.name

    try:
        data = _yaml.load(inv.registry_path.read_text())
    except (YAMLError, OSError) as exc:
        line = (
            exc.problem_mark.line + 1
            if isinstance(exc, MarkedYAMLError) and exc.problem_mark
            else 0
        )
        diags.error(codes.E001, f"registry.yml is unparseable: {exc}", span=span_at(file, line))
        return Registry()

    if data is None:
        return Registry()  # empty file: no entities
    if not isinstance(data, Mapping):
        diags.error(
            codes.E001,
            "registry.yml must be a mapping of sandboxes/agents/events",
            span=span_at(file, 1),
        )
        return Registry()

    defaults = data.get("defaults") or {}
    default_agent = (defaults.get("agent") or {}) if isinstance(defaults, Mapping) else {}

    sandboxes = _load_sandboxes(data.get("sandboxes") or {}, file)
    agents = _load_agents(data.get("agents") or {}, default_agent, file)
    events = _load_events(data.get("events") or {}, file, diags)

    _check_naming(sandboxes, agents, events, diags)

    return Registry(sandboxes=sandboxes, agents=agents, events=events)


def _load_sandboxes(sb_map: Mapping, file: str) -> dict[str, Sandbox]:
    out: dict[str, Sandbox] = {}
    for name, body in sb_map.items():
        body = body or {}
        out[name] = Sandbox(
            name=name,
            provider=body.get("provider"),
            image=dict(body.get("image") or {}),
            network=list(body.get("network") or []),
            env_file=_as_str_list(body.get("env_file")),  # path reference only; not read here
            span=span_at(file, _line_of(sb_map, name)),
        )
    return out


def _load_agents(ag_map: Mapping, default_agent: Mapping, file: str) -> dict[str, Agent]:
    out: dict[str, Agent] = {}
    for name, body in ag_map.items():
        body = body or {}
        # harness: deep-merge field-by-field (agent overrides defaults).
        harness = {**(default_agent.get("harness") or {}), **(body.get("harness") or {})}
        # sandbox: scalar override, else inherit.
        sandbox = body.get("sandbox", default_agent.get("sandbox"))
        # skills: the agent's own list replaces the default wholesale (no union).
        skills = (
            list(body["skills"]) if "skills" in body else list(default_agent.get("skills") or [])
        )
        out[name] = Agent(
            name=name,
            harness=Harness(**harness),
            sandbox=sandbox,
            skills=skills,
            span=span_at(file, _line_of(ag_map, name)),
        )
    return out


def _load_events(ev_map: Mapping, file: str, diags: DiagnosticCollector) -> dict[str, Event]:
    out: dict[str, Event] = {}
    for name, body in ev_map.items():
        body = body or {}
        raw_fields = body.get("fields") or {}
        fields: dict[str, dict] = {}
        for fname, ftype in raw_fields.items():
            schema = desugar(ftype, span=span_at(file, _line_of(raw_fields, fname)), diags=diags)
            if schema is not None:
                fields[fname] = schema
        out[name] = Event(name=name, fields=fields, span=span_at(file, _line_of(ev_map, name)))
    return out


def _check_naming(
    sandboxes: dict[str, Sandbox],
    agents: dict[str, Agent],
    events: dict[str, Event],
    diags: DiagnosticCollector,
) -> None:
    # X1: entities Capitalized; `default` is the one reserved lowercase sandbox.
    for name, sb in sandboxes.items():
        if name == "default":
            continue
        if not _is_capitalized(name):
            diags.error(
                codes.E210,
                f"sandbox '{name}' must be Capitalized (or be the reserved 'default')",
                span=sb.span,
            )
    for label, mapping in (("agent", agents), ("event", events)):
        for name, node in mapping.items():
            if name == "default":
                diags.error(
                    codes.E210,
                    f"'default' is reserved for the default sandbox; "
                    f"an {label} cannot be named 'default'",
                    span=node.span,
                )
            elif not _is_capitalized(name):
                diags.error(codes.E210, f"{label} '{name}' must be Capitalized", span=node.span)

    # X1: no duplicate names across the entity namespace.
    seen: dict[str, list[str]] = defaultdict(list)
    for category, mapping in (("sandbox", sandboxes), ("agent", agents), ("event", events)):
        for name in mapping:
            seen[name].append(category)
    for name, cats in seen.items():
        if len(cats) > 1:
            node = agents.get(name) or events.get(name) or sandboxes.get(name)
            span: Span | None = node.span if node else None
            diags.error(
                codes.E211,
                f"duplicate entity name '{name}' used as {' and '.join(cats)}",
                span=span,
            )
