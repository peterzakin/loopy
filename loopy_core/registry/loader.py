"""P1 — parse registry.yml into a typed Registry with defaults applied (M1)."""

from __future__ import annotations

from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry


def load_registry(inv: Inventory, diags: DiagnosticCollector) -> Registry:
    """M0 skeleton: empty registry. M1 parses with ruamel.yaml, applies
    `defaults.agent` inheritance, desugars event field types, and runs X1 naming."""
    return Registry()
