"""P4 — build a networkx.DiGraph per workflow from `after:` edges and run W2–W7.

The `on:` step is the root; loop-backs go through events, never `after:`, so the
`after:`-graph must be acyclic.
"""

from __future__ import annotations

import networkx as nx

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.workflow.model import Workflow


def build_dag(workflow: Workflow, diags: DiagnosticCollector) -> None:
    steps = workflow.steps
    graph = nx.DiGraph()
    for name in steps:
        graph.add_node(name)

    # W2 — exactly one entry (on:) step.
    entries = [s for s in steps.values() if s.trigger is not None]
    if len(entries) == 1:
        workflow.entry = entries[0].name
    elif steps:
        span = next(iter(steps.values())).span
        diags.error(
            codes.E102,
            f"workflow '{workflow.name}' must have exactly one entry (on:) step; "
            f"found {len(entries)}",
            span=span,
        )

    for name, step in steps.items():
        has_on = step.trigger is not None
        has_after = bool(step.after)

        # W4 — on: and after: are mutually exclusive.
        if has_on and has_after:
            diags.error(
                codes.E104,
                f"step '{step.id}' has both on: and after: (mutually exclusive)",
                span=step.span,
            )
        # W3 — a step with neither on: nor after: is an orphan.
        if not has_on and not has_after:
            diags.error(
                codes.E103, f"orphan step '{step.id}' — neither on: nor after:", span=step.span
            )

        # W5 — after: targets must exist; build edges for the ones that do.
        for target in step.after:
            if target not in steps:
                diags.error(
                    codes.E105,
                    f"step '{step.id}' has after: '{target}', "
                    f"which is not a step in '{workflow.name}'",
                    span=step.span,
                )
            else:
                graph.add_edge(target, name)

    # W6 — the after:-graph must be acyclic.
    try:
        cycle = nx.find_cycle(graph)
        path = " -> ".join(src for src, _ in cycle)
        diags.error(
            codes.E106,
            f"after: cycle in workflow '{workflow.name}': {path}",
            span=next(iter(steps.values())).span if steps else None,
        )
    except nx.NetworkXNoCycle:
        pass

    workflow.dag = graph
