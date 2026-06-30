"""The compile pipeline: discover -> parse -> resolve -> validate -> emit (FRONTEND §1).

Each pass accumulates diagnostics and never fails fast. M0 wires the P0–P9 skeleton
as no-ops over the stage stubs; later milestones fill each stage in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loopy_core.compile.builtins import inject_builtins
from loopy_core.compile.crosscheck import cross_check
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.compile.model import Lineage, Project
from loopy_core.discovery import discover
from loopy_core.registry.loader import load_registry
from loopy_core.sensors.loader import load_sensors
from loopy_core.template.resolver import resolve_refs
from loopy_core.workflow.loader import load_workflows


@dataclass
class CompileResult:
    project: Project | None
    diagnostics: DiagnosticCollector


def compile_project(root: str | Path) -> CompileResult:
    """Run the full pipeline over a project directory, returning the IR `Project`
    (when one could be built) and every accumulated diagnostic."""
    diags = DiagnosticCollector()
    root = Path(root)

    # P0 — discovery
    inv = discover(root)
    # P1–P2 — registry + type desugar
    registry = load_registry(inv, diags)
    # P3–P5 — workflows: frontmatter, steps, DAG, template-ref extraction
    workflows = load_workflows(inv, registry, diags)
    # P7 — sensors (loaded before P6 so the built-in pass can guard the reserved namespace
    # over both user events and user sensors before refs resolve)
    user_sensors = load_sensors(inv, registry, diags)
    # Built-ins — register referenced Github.* event contracts + synthesize their sensors,
    # so P6 ref-resolution and X3/X5 see them as ordinary registered/produced events.
    builtin_sensors = inject_builtins(registry, workflows, user_sensors, diags)
    # P6 — statically resolve template refs against events + after: predecessors
    resolve_refs(workflows, registry, diags)

    project = Project(
        registry=registry,
        workflows=workflows,
        sensors=user_sensors + builtin_sensors,
        lineage=Lineage(),  # populated by X5 in cross_check
    )

    # P8 — whole-IR cross-cutting checks (X2/X3) + lineage (X5)
    cross_check(project, inv, diags)

    return CompileResult(project=project, diagnostics=diags)
