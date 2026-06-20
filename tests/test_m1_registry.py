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


def test_registry_limits_cascade_spend_parses(tmp_path):
    write_project(tmp_path, {"registry.yml": MINIMAL + "limits:\n  cascade_spend:\n    usd: 50\n"})
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    assert result.project.registry.limits.cascade_spend == {"usd": 50.0}


def test_limits_cascade_spend_without_numeric_usd_reports_e213(tmp_path):
    write_project(
        tmp_path, {"registry.yml": MINIMAL + "limits:\n  cascade_spend:\n    usd: lots\n"}
    )
    result = compile_project(tmp_path)
    assert_code(result, codes.E213, file="registry.yml")


def test_limits_per_workflow_spend_parses(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": MINIMAL
            + "events:\n  Go:\n    fields: {}\n"
            + "limits:\n  workflows:\n    triage:\n      spend:\n        usd: 10\n",
            "workflows/triage/step.md": "---\non: Go\nagent: Worker\n---\nbody\n",
        },
    )
    result = compile_project(tmp_path)
    assert not result.diagnostics.has_errors()  # only a W501 dead-trigger warning
    assert result.project.registry.limits.workflows["triage"].spend == {"usd": 10.0}


def test_limits_workflows_unknown_workflow_reports_e505(tmp_path):
    reg = MINIMAL + "limits:\n  workflows:\n    nope:\n      spend:\n        usd: 1\n"
    write_project(tmp_path, {"registry.yml": reg})
    result = compile_project(tmp_path)
    assert_code(result, codes.E505, file="registry.yml")


def test_defaults_deep_merge_harness_replace_lists(tmp_path):
    registry = (
        "defaults:\n"
        "  agent:\n"
        "    sandbox: default\n"
        "    harness: { runtime: claude-code, model: claude-sonnet-4-6 }\n"
        "    skills: [base_skill]\n"
        "sandboxes:\n"
        "  default:\n"
        "    provider: daytona\n"
        "agents:\n"
        "  Investigator: {}\n"
        "  Fixer: { harness: { model: claude-opus-4-8 }, skills: [testing] }\n"
    )
    write_project(
        tmp_path,
        {
            "registry.yml": registry,
            "skills/base_skill/SKILL.md": "x",
            "skills/testing/SKILL.md": "x",
        },
    )
    agents = compile_project(tmp_path).project.registry.agents

    # Investigator inherits the whole default harness, sandbox, and skills.
    inv = agents["Investigator"]
    assert inv.harness.runtime == "claude-code"
    assert inv.harness.model == "claude-sonnet-4-6"
    assert inv.sandbox == "default"
    assert inv.skills == ["base_skill"]

    # Fixer: harness deep-merges (keeps inherited runtime, overrides model);
    # skills REPLACE the default list (no union).
    fixer = agents["Fixer"]
    assert fixer.harness.runtime == "claude-code"
    assert fixer.harness.model == "claude-opus-4-8"
    assert fixer.skills == ["testing"]


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


# ── Sandbox repos: clone targets (E212) ─────────────────────────────────────────


def test_repos_parse_string_shorthand_and_object_form(tmp_path):
    registry = (
        "sandboxes:\n"
        "  default:\n"
        "    provider: local\n"
        "    repos:\n"
        "      - acme/runbooks\n"
        "      - { url: acme/playbooks, ref: main, path: pb, depth: null }\n"
    )
    write_project(tmp_path, {"registry.yml": registry})
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    repos = result.project.registry.sandboxes["default"].repos
    assert [(r.url, r.ref, r.path, r.depth) for r in repos] == [
        ("acme/runbooks", None, None, 1),
        ("acme/playbooks", "main", "pb", None),
    ]


def test_repos_missing_url_reports_e212(tmp_path):
    registry = "sandboxes:\n  default:\n    repos:\n      - { ref: main }\n"
    write_project(tmp_path, {"registry.yml": registry})
    assert_code(compile_project(tmp_path), codes.E212)


def test_repos_non_list_reports_e212(tmp_path):
    registry = "sandboxes:\n  default:\n    repos: acme/runbooks\n"
    write_project(tmp_path, {"registry.yml": registry})
    assert_code(compile_project(tmp_path), codes.E212)


# ── Sandbox provider is required (E214) ──────────────────────────────────────────


def test_sandbox_without_provider_reports_e214(tmp_path):
    # `provider:` is mandatory on every sandbox — where an agent runs is never inferred.
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  default:\n    image: {}\n"})
    assert_code(compile_project(tmp_path), codes.E214, file="registry.yml")


def test_sandbox_with_provider_does_not_report_e214(tmp_path):
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  default:\n    provider: daytona\n"})
    assert codes.E214 not in result_codes(compile_project(tmp_path))
