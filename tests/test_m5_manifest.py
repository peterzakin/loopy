"""M5: cross-cutting checks (E501–E504, W501), manifest, schema, golden + properties."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from loopy_core.compile import codes
from loopy_core.compile.crosscheck import cross_check
from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.model import Project
from loopy_core.compile.pipeline import compile_project
from loopy_core.discovery import discover
from loopy_core.registry.loader import load_registry
from loopy_core.sensors.loader import load_sensors
from loopy_core.workflow.loader import load_workflows
from tests.helpers import assert_code, write_project

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "incidents"
GOLDEN = Path(__file__).resolve().parent / "golden" / "incidents.manifest.json"
SCHEMA = Path(__file__).resolve().parents[1] / "loopy_core" / "manifest.schema.json"


# ── Golden positive ───────────────────────────────────────────────────────────


def test_example_compiles_clean():
    result = compile_project(EXAMPLE)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]


def test_example_manifest_matches_golden_snapshot():
    result = compile_project(EXAMPLE)
    assert to_manifest(result.project) == json.loads(GOLDEN.read_text())


def test_manifest_validates_against_schema():
    result = compile_project(EXAMPLE)
    jsonschema.validate(to_manifest(result.project), json.loads(SCHEMA.read_text()))


# ── Properties ─────────────────────────────────────────────────────────────────


def test_manifest_is_idempotent():
    result = compile_project(EXAMPLE)
    assert to_manifest(result.project) == to_manifest(result.project)


def _compile_with_reversed_discovery() -> dict:
    """Compile the example but feed the loaders file lists in reversed order."""
    inv = discover(EXAMPLE)
    inv.workflow_files.reverse()
    inv.sensor_files.reverse()
    from loopy_core.compile.diagnostics import DiagnosticCollector

    diags = DiagnosticCollector()
    registry = load_registry(inv, diags)
    workflows = load_workflows(inv, registry, diags)
    sensors = load_sensors(inv, registry, diags)
    project = Project(registry=registry, workflows=workflows, sensors=sensors)
    cross_check(project, inv, diags)
    return to_manifest(project)


def test_manifest_is_order_independent():
    forward = to_manifest(compile_project(EXAMPLE).project)
    reversed_ = _compile_with_reversed_discovery()
    assert json.dumps(forward, sort_keys=True) == json.dumps(reversed_, sort_keys=True)


# ── Cross-cutting negatives (X2/X3/X5) ──────────────────────────────────────────

REGISTRY = (
    "sandboxes:\n  default:\n    provider: local\n"
    "agents:\n  Worker: { sandbox: default }\n"
    "events:\n  Incident:\n    fields:\n      issue_id: id\n"
)


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def test_unregistered_step_agent_reports_e501(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Ghost"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E501)


def test_unresolved_agent_sandbox_reports_e502(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": "agents:\n  Worker: { sandbox: Ghostbox }\n"},
    )
    assert_code(compile_project(tmp_path), codes.E502)


def test_unresolved_skill_reports_e503(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": "agents:\n  Worker: { skills: [ghost-skill] }\n"},
    )
    assert_code(compile_project(tmp_path), codes.E503)


def test_unregistered_event_reports_e504(tmp_path):
    # Incident is NOT registered here, but a step triggers on it.
    write_project(
        tmp_path,
        {
            "registry.yml": "agents:\n  Worker: {}\n",
            "workflows/w/a.md": md("on: Incident\nagent: Worker"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E504)


def test_dead_trigger_reports_w501(tmp_path):
    registry = "agents:\n  Worker: {}\nevents:\n  Lonely:\n    fields:\n      x: str\n"
    write_project(
        tmp_path,
        {
            "registry.yml": registry,
            "workflows/w/a.md": md("on: Lonely\nagent: Worker"),
        },
    )
    assert_code(compile_project(tmp_path), codes.W501)


# ── Coverage: every §14 code is producible somewhere in the suite ────────────────


def test_golden_snapshot_is_committed():
    # Guards against an accidental missing/empty golden file.
    assert GOLDEN.is_file()
    assert json.loads(GOLDEN.read_text())["schema_version"] == "1"


def test_every_section14_code_has_a_fixture():
    """The golden-negative contract: every §14 code is exercised by some test."""
    import re

    from loopy_core.compile import codes as codes_mod

    names = [n for n in dir(codes_mod) if re.fullmatch(r"[EW]\d+", n)]
    test_dir = Path(__file__).resolve().parent
    referenced: set[str] = set()
    for test_file in test_dir.glob("test_*.py"):
        text = test_file.read_text()
        referenced.update(n for n in names if f"codes.{n}" in text)
    missing = set(names) - referenced
    assert not missing, f"§14 codes with no fixture/test: {sorted(missing)}"
