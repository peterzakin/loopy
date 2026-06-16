"""M1: discovery, registry loader (defaults merge + spans), type desugar, X1 naming."""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.pipeline import compile_project
from loopy_core.discovery import discover
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

MINIMAL = "sandboxes:\n  default:\n    provider: local\nagents:\n  Worker: {}\n"


def test_minimal_registry_compiles_clean(tmp_path):
    write_project(tmp_path, {"registry.yml": MINIMAL})
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    assert "Worker" in result.project.registry.agents


def test_missing_registry_reports_e001(tmp_path):
    write_project(tmp_path, {"workflows/triage/investigate.md": "x"})
    result = compile_project(tmp_path)
    assert_code(result, codes.E001)


def test_unparseable_registry_reports_e001(tmp_path):
    write_project(tmp_path, {"registry.yml": "agents: [unclosed\n"})
    result = compile_project(tmp_path)
    assert_code(result, codes.E001, file="registry.yml")


def test_defaults_deep_merge_harness_replace_lists(tmp_path):
    registry = (
        "defaults:\n"
        "  agent:\n"
        "    sandbox: default\n"
        "    harness: { runtime: claude-code, model: claude-sonnet-4-6 }\n"
        "    tools: [base_tool]\n"
        "sandboxes:\n"
        "  default:\n"
        "    provider: daytona\n"
        "agents:\n"
        "  Investigator: { skills: [triage] }\n"
        "  Fixer: { harness: { model: claude-opus-4-8 }, tools: [open_pr] }\n"
    )
    write_project(tmp_path, {"registry.yml": registry})
    agents = compile_project(tmp_path).project.registry.agents

    # Investigator inherits the whole default harness, sandbox, and tools.
    inv = agents["Investigator"]
    assert inv.harness.runtime == "claude-code"
    assert inv.harness.model == "claude-sonnet-4-6"
    assert inv.sandbox == "default"
    assert inv.tools == ["base_tool"]

    # Fixer: harness deep-merges (keeps inherited runtime, overrides model);
    # tools REPLACE the default list (no union).
    fixer = agents["Fixer"]
    assert fixer.harness.runtime == "claude-code"
    assert fixer.harness.model == "claude-opus-4-8"
    assert fixer.tools == ["open_pr"]


def test_type_desugar_table(tmp_path):
    registry = (
        "events:\n"
        "  Incident:\n"
        "    fields:\n"
        "      source: enum[sentry, linear]\n"
        "      issue_id: id\n"
        "      link: url\n"
        "      count: int\n"
    )
    write_project(tmp_path, {"registry.yml": registry})
    fields = compile_project(tmp_path).project.registry.events["Incident"].fields
    assert fields["source"] == {"type": "string", "enum": ["sentry", "linear"]}
    assert fields["issue_id"] == {"type": "string", "format": "loopy-id"}
    assert fields["link"] == {"type": "string", "format": "uri"}
    assert fields["count"] == {"type": "integer"}


def test_raw_json_schema_passes_through(tmp_path):
    registry = "events:\n  E:\n    fields:\n      tags: { type: array, items: { type: string } }\n"
    write_project(tmp_path, {"registry.yml": registry})
    fields = compile_project(tmp_path).project.registry.events["E"].fields
    assert fields["tags"] == {"type": "array", "items": {"type": "string"}}


def test_unknown_type_shorthand_reports_e201_at_line(tmp_path):
    # x: is on line 4 of the file.
    registry = "events:\n  Bad:\n    fields:\n      x: notatype\n"
    write_project(tmp_path, {"registry.yml": registry})
    result = compile_project(tmp_path)
    assert_code(result, codes.E201, file="registry.yml", line=4)


def test_naming_lowercase_agent_reports_e210(tmp_path):
    write_project(tmp_path, {"registry.yml": "agents:\n  worker: {}\n"})
    assert_code(compile_project(tmp_path), codes.E210)


def test_naming_lowercase_sandbox_reports_e210(tmp_path):
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  mybox:\n    provider: local\n"})
    assert_code(compile_project(tmp_path), codes.E210)


def test_default_sandbox_is_allowed(tmp_path):
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  default:\n    provider: local\n"})
    assert codes.E210 not in result_codes(compile_project(tmp_path))


def test_reserved_default_as_event_reports_e210(tmp_path):
    write_project(tmp_path, {"registry.yml": "events:\n  default:\n    fields: {}\n"})
    assert_code(compile_project(tmp_path), codes.E210)


def test_duplicate_name_across_categories_reports_e211(tmp_path):
    registry = "agents:\n  Incident: {}\nevents:\n  Incident:\n    fields: {}\n"
    write_project(tmp_path, {"registry.yml": registry})
    assert_code(compile_project(tmp_path), codes.E211)


def test_discovery_inventories_all_four_kinds(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": MINIMAL,
            "workflows/triage/investigate.md": "x",
            "skills/triage/SKILL.md": "x",
            "sensors/sensors.py": "x",
        },
    )
    inv = discover(tmp_path)
    assert inv.registry_path is not None
    assert [p.name for p in inv.workflow_files] == ["investigate.md"]
    assert [p.name for p in inv.skill_dirs] == ["triage"]
    assert [p.name for p in inv.sensor_files] == ["sensors.py"]
