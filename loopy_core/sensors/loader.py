"""P7 — Python AST sensor inspector. Reads each sensor's declared `emits` + trigger
statically (never imports), runs S1–S3, and populates one `Sensor` each (M4)."""

from __future__ import annotations

from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry
from loopy_core.sensors.model import Sensor


def load_sensors(inv: Inventory, registry: Registry, diags: DiagnosticCollector) -> list[Sensor]:
    """M0 skeleton: no sensors. M4 parses sensors/*.py to an AST and validates them."""
    return []
