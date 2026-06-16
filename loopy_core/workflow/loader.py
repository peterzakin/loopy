"""P3 — build `Step`s from workflow `.md` files; normalize after:/emits:; validate
on: shape; desugar step output: maps (M2)."""

from __future__ import annotations

from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry
from loopy_core.workflow.model import Workflow


def load_workflows(
    inv: Inventory, registry: Registry, diags: DiagnosticCollector
) -> dict[str, Workflow]:
    """M0 skeleton: no workflows. M2 parses frontmatter, builds steps, and the DAG."""
    return {}
