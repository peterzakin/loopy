"""P3 — build `Step`s from workflow `.md` files and assemble per-workflow DAGs.

Step identity is `<workflow_dir>/<filename_stem>`; `after:`/`emits:` normalize to
lists; `on:` is a single event or `cron("...")` (a list -> E111); step `output:`
maps desugar via the M1 type pass. The DAG rules (W2–W7) run in `dag.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry
from loopy_core.registry.types import desugar
from loopy_core.span import Span, span_at
from loopy_core.template.parser import extract_refs
from loopy_core.workflow.dag import build_dag
from loopy_core.workflow.frontmatter import ParsedDoc, parse_document
from loopy_core.workflow.model import Budget, Step, Trigger, Workflow

# cron("<quoted expr>"[, tz=<iana>])
_CRON_RE = re.compile(r'^cron\(\s*"(?P<expr>[^"]*)"\s*(?:,\s*tz\s*=\s*(?P<tz>[^)]+?)\s*)?\)$')

# <Event>(<args>) — an event trigger with filters, e.g. Github.PullRequestOpened(repo="a/b")
_FILTERED_EVENT_RE = re.compile(r"^(?P<event>[A-Za-z_][\w.]*)\((?P<args>.*)\)$", re.DOTALL)
# one filter arg: field="value" (values are always quoted; fields are contract field names)
_FILTER_ARG_RE = re.compile(r'^(?P<key>[A-Za-z_]\w*)\s*=\s*"(?P<value>[^"]*)"$')


def load_workflows(
    inv: Inventory, registry: Registry, diags: DiagnosticCollector
) -> dict[str, Workflow]:
    workflows: dict[str, Workflow] = {}
    seen_ids: set[str] = set()

    for path in inv.workflow_files:
        try:
            file = str(path.relative_to(inv.root))
        except ValueError:
            file = path.name
        wf_name = path.parent.name
        step_name = path.stem
        step_id = f"{wf_name}/{step_name}"

        doc = parse_document(path.read_text())
        if doc is None or not doc.body.strip():
            diags.error(
                codes.E101,
                f"{file}: invalid frontmatter or empty body",
                span=span_at(file, 1),
            )
            continue

        # W7 (global id uniqueness — structurally rare, but enforced).
        if step_id in seen_ids:
            diags.error(codes.E107, f"duplicate step id '{step_id}'", span=span_at(file, 1))
            continue
        seen_ids.add(step_id)

        step = _build_step(file, wf_name, step_name, step_id, doc, diags)
        wf = workflows.setdefault(wf_name, Workflow(name=wf_name))
        wf.steps[step_name] = step

    for wf in workflows.values():
        build_dag(wf, diags)

    return workflows


def _build_step(
    file: str,
    wf_name: str,
    step_name: str,
    step_id: str,
    doc: ParsedDoc,
    diags: DiagnosticCollector,
) -> Step:
    meta = doc.meta
    trigger = _parse_trigger(meta.get("on"), span_at(file, doc.line_of("on")), diags)

    output: dict[str, dict] = {}
    raw_output = meta.get("output") or {}
    if isinstance(raw_output, Mapping):
        for key, type_ in raw_output.items():
            schema = desugar(type_, span=span_at(file, doc.abs_line(raw_output, key)), diags=diags)
            if schema is not None:
                output[key] = schema

    budget = None
    raw_budget = meta.get("budget")
    if isinstance(raw_budget, Mapping):
        budget = Budget(
            wall_clock=raw_budget.get("wall_clock"),
            spend=dict(raw_budget["spend"])
            if isinstance(raw_budget.get("spend"), Mapping)
            else None,
            window=raw_budget.get("window"),
            latency=raw_budget.get("latency"),
        )

    # P5 — extract template refs now (body start line is known here for accurate spans).
    refs = extract_refs(doc.body, doc.body_start_line, file, diags)

    return Step(
        id=step_id,
        workflow=wf_name,
        name=step_name,
        trigger=trigger,
        after=_as_list(meta.get("after")),
        agent=meta.get("agent"),
        output=output,
        emits=_as_list(meta.get("emits")),
        budget=budget,
        body=doc.body,
        refs=refs,
        span=span_at(file, 1),
    )


def _parse_trigger(on: object, span: Span, diags: DiagnosticCollector) -> Trigger | None:
    if on is None:
        return None
    if isinstance(on, list):
        diags.error(
            codes.E111,
            "on: must name exactly one event or a cron(...) trigger, not a list",
            span=span,
            hint="fan-in happens at the sensor layer; have several sensors emit one event",
        )
        return None
    text = str(on).strip()
    if text.startswith("cron("):
        return _parse_cron(text, span, diags)
    if "(" in text or ")" in text:
        return _parse_filtered_event(text, span, diags)
    return Trigger(kind="event", event=text, span=span)


def _parse_filtered_event(value: str, span: Span, diags: DiagnosticCollector) -> Trigger | None:
    """Parse `Event(field="value", ...)` — an event trigger scoped by field filters.

    Every filter must match the published event's field exactly (AND semantics); which
    fields exist — and that they are strings — is checked against the event's contract in
    `cross_check` (E114), not here. Malformed syntax is E113. Bare `on: Event` stays the
    unfiltered form: every published instance triggers.
    """

    def malformed(reason: str) -> None:
        diags.error(
            codes.E113,
            f"malformed event trigger: {value!r} — {reason}",
            span=span,
            hint='filters are field="quoted value" pairs, e.g. '
            'Github.PullRequestOpened(repo="octocat/Hello-World")',
        )

    match = _FILTERED_EVENT_RE.match(value)
    if not match:
        malformed('expected <Event>(field="value", ...)')
        return None
    event, args = match.group("event"), match.group("args").strip()
    if not args:
        malformed("empty filter list; drop the parentheses to trigger on every instance")
        return None

    filters: dict[str, str] = {}
    for raw in _split_filter_args(args):
        arg = _FILTER_ARG_RE.match(raw.strip())
        if not arg:
            malformed(f"bad filter {raw.strip()!r}")
            return None
        key = arg.group("key")
        if key in filters:
            malformed(f"duplicate filter field '{key}'")
            return None
        filters[key] = arg.group("value")
    return Trigger(kind="event", event=event, filters=filters, span=span)


def _split_filter_args(args: str) -> list[str]:
    """Split a filter arg list on commas outside quotes (values may contain commas)."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in args:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_cron(value: str, span: Span, diags: DiagnosticCollector) -> Trigger | None:
    match = _CRON_RE.match(value)
    if not match:
        diags.error(
            codes.E110,
            f"malformed cron trigger: {value!r}",
            span=span,
            hint='quote the expression, e.g. cron("0 3 * * *") or cron("1,15 * * * *", tz=UTC)',
        )
        return None
    expr = match.group("expr")
    tz = match.group("tz").strip() if match.group("tz") else None

    if len(expr.split()) != 5 or not croniter.is_valid(expr):
        diags.error(codes.E110, f"invalid cron expression: {expr!r} (expects 5 fields)", span=span)
        return None

    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            diags.error(codes.E110, f"unknown timezone: {tz!r}", span=span)
            return None

    return Trigger(kind="cron", expr=expr, tz=tz, span=span)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
