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
from loopy_core.workflow.dag import build_dag
from loopy_core.workflow.frontmatter import ParsedDoc, parse_document
from loopy_core.workflow.model import Budget, Step, Trigger, Workflow

# cron("<quoted expr>"[, tz=<iana>])
_CRON_RE = re.compile(r'^cron\(\s*"(?P<expr>[^"]*)"\s*(?:,\s*tz\s*=\s*(?P<tz>[^)]+?)\s*)?\)$')


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
    return Trigger(kind="event", event=text, span=span)


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
