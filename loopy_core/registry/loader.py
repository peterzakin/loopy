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
from loopy_core.registry.model import (
    Agent,
    Event,
    Harness,
    Limits,
    Registry,
    Repo,
    Sandbox,
    WorkflowLimit,
)
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

    sandboxes = _load_sandboxes(data.get("sandboxes") or {}, file, diags)
    agents = _load_agents(data.get("agents") or {}, default_agent, file)
    events = _load_events(data.get("events") or {}, file, diags)
    limits = _load_limits(data.get("limits"), file, _line_of(data, "limits"), diags)

    _check_naming(sandboxes, agents, events, diags)

    return Registry(sandboxes=sandboxes, agents=agents, events=events, limits=limits)


def _spend_usd(raw: object) -> float | None:
    """A `{ usd: <number> }` spend block's USD value, or None if absent/malformed."""
    if not isinstance(raw, Mapping):
        return None
    usd = raw.get("usd")
    if not isinstance(usd, (int, float)) or isinstance(usd, bool):
        return None
    return float(usd)


def _load_limits(value: object, file: str, line: int, diags: DiagnosticCollector) -> Limits | None:
    """Parse the `limits:` block — the project-wide `cascade_spend: { usd }` and per-named-workflow
    `workflows.<Name>.spend: { usd }`. A malformed shape is E213; whether a named workflow exists
    is a cross-check (E505), since workflows aren't loaded yet here."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        diags.error(codes.E213, "registry 'limits' must be a mapping", span=span_at(file, line))
        return None

    cascade_spend = None
    raw_cascade = value.get("cascade_spend")
    if raw_cascade is not None:
        usd = _spend_usd(raw_cascade)
        if usd is None:
            diags.error(
                codes.E213,
                "registry 'limits.cascade_spend' must be a mapping with a numeric 'usd'",
                span=span_at(file, _line_of(value, "cascade_spend") or line),
            )
        else:
            cascade_spend = {"usd": usd}

    workflows: dict[str, WorkflowLimit] = {}
    raw_wfs = value.get("workflows")
    if raw_wfs is not None:
        wf_line = _line_of(value, "workflows") or line
        if not isinstance(raw_wfs, Mapping):
            diags.error(
                codes.E213,
                "registry 'limits.workflows' must be a mapping",
                span=span_at(file, wf_line),
            )
        else:
            for name, body in raw_wfs.items():
                usd = _spend_usd(body.get("spend")) if isinstance(body, Mapping) else None
                if usd is None:
                    diags.error(
                        codes.E213,
                        f"registry 'limits.workflows.{name}' must have spend: {{ usd: <number> }}",
                        span=span_at(file, _line_of(raw_wfs, name) or wf_line),
                    )
                    continue
                workflows[str(name)] = WorkflowLimit(spend={"usd": usd})

    if cascade_spend is None and not workflows:
        return None
    return Limits(cascade_spend=cascade_spend, workflows=workflows)


def _load_sandboxes(
    sb_map: Mapping, file: str, diags: DiagnosticCollector
) -> dict[str, Sandbox]:
    out: dict[str, Sandbox] = {}
    for name, body in sb_map.items():
        body = body or {}
        provider = body.get("provider")
        # `provider:` is required on every sandbox — where an agent runs is an explicit
        # property of the project, never inferred. (`loopy init` scaffolds `daytona`.)
        if not isinstance(provider, str) or not provider.strip():
            diags.error(
                codes.E214,
                f"sandbox '{name}' must declare a provider: (local | docker | daytona)",
                span=span_at(file, _line_of(sb_map, name)),
            )
        out[name] = Sandbox(
            name=name,
            provider=provider,
            image=dict(body.get("image") or {}),
            network=list(body.get("network") or []),
            env_file=_as_str_list(body.get("env_file")),  # path reference only; not read here
            repos=_load_repos(body.get("repos"), file, _line_of(sb_map, name), diags),
            span=span_at(file, _line_of(sb_map, name)),
        )
    return out


def _load_repos(value: object, file: str, line: int, diags: DiagnosticCollector) -> list[Repo]:
    """Normalize the `repos:` list to `Repo`s. Each item is an `owner/name`/URL string or a
    mapping with at least `url`; anything else is E212. Returns the well-formed subset."""
    if value is None:
        return []
    if not isinstance(value, list):
        diags.error(codes.E212, "sandbox 'repos' must be a list of repos", span=span_at(file, line))
        return []
    repos: list[Repo] = []
    for item in value:
        if isinstance(item, str):
            if not item.strip():
                diags.error(codes.E212, "a repo entry is an empty string", span=span_at(file, line))
                continue
            repos.append(Repo(url=item.strip()))
        elif isinstance(item, Mapping):
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                diags.error(
                    codes.E212, "a repo entry is missing a 'url'", span=span_at(file, line)
                )
                continue
            depth = item.get("depth", 1)
            repos.append(
                Repo(
                    url=url.strip(),
                    ref=item.get("ref"),
                    path=item.get("path"),
                    depth=int(depth) if depth is not None else None,
                )
            )
        else:
            diags.error(
                codes.E212,
                "a repo entry must be an 'owner/name' string or a {url, ...} mapping",
                span=span_at(file, line),
            )
    return repos


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
