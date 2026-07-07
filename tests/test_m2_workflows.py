"""M2: frontmatter + step model + DAG (W1–W7), cron parsing, output desugar."""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.compile.pipeline import compile_project
from loopy_core.discovery import Inventory
from loopy_core.registry.model import Registry
from loopy_core.workflow.loader import load_workflows
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

REGISTRY = (
    "defaults: { agent: { sandbox: default, model: claude-sonnet-4-6, harness: claude-code } }\n"
    "sandboxes: { default: { provider: local } }\n"
    "agents: { Investigator: {} }\n"
    "events:\n"
    "  Incident: { issue_id: str }\n"
    "  WorkItem: { note: str }\n"
    "  MetricThreshold: { goal_id: str }\n"
)

M2_CODES = {
    codes.E101,
    codes.E102,
    codes.E103,
    codes.E104,
    codes.E105,
    codes.E106,
    codes.E107,
    codes.E110,
    codes.E111,
    codes.E113,
}


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def assert_no_m2_errors(result) -> None:
    fired = set(result_codes(result)) & M2_CODES
    assert not fired, f"unexpected M2 codes: {fired}"


def test_single_step_workflow_builds_clean(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md(
                "on: Incident\nagent: Investigator\nemits: WorkItem"
            ),
        },
    )
    result = compile_project(tmp_path)
    assert_no_m2_errors(result)
    wf = result.project.workflows["triage"]
    assert wf.entry == "investigate"
    step = wf.steps["investigate"]
    assert step.id == "triage/investigate"
    assert step.trigger.kind == "event"
    assert step.trigger.event == "Incident"
    assert step.emits == ["WorkItem"]


def test_multi_step_dag_edges_and_entry(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/resolve/arbitrate.md": md("on: WorkItem\nagent: Investigator"),
            "workflows/resolve/fix.md": md("after: arbitrate\nagent: Investigator"),
        },
    )
    result = compile_project(tmp_path)
    assert_no_m2_errors(result)
    wf = result.project.workflows["resolve"]
    assert wf.entry == "arbitrate"
    assert wf.dag.has_edge("arbitrate", "fix")


def test_after_and_emits_normalize_to_lists(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator"),
            "workflows/w/b.md": md("after: a\nemits: WorkItem\nagent: Investigator"),
            "workflows/w/c.md": md("after: [a, b]\nagent: Investigator"),
        },
    )
    wf = compile_project(tmp_path).project.workflows["w"]
    assert wf.steps["b"].after == ["a"]
    assert wf.steps["b"].emits == ["WorkItem"]
    assert wf.steps["c"].after == ["a", "b"]


def test_step_output_desugars(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md(
                "on: Incident\nagent: Investigator\noutput:\n  verdict: enum[pass, fail]"
            ),
        },
    )
    wf = compile_project(tmp_path).project.workflows["w"]
    assert wf.steps["a"].output["verdict"] == {"type": "string", "enum": ["pass", "fail"]}


def test_on_list_reports_e111(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: [Incident, WorkItem]\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E111, file="workflows/w/a.md")


def test_invalid_frontmatter_reports_e101(tmp_path):
    write_project(tmp_path, {"registry.yml": REGISTRY, "workflows/w/a.md": "no frontmatter here"})
    assert_code(compile_project(tmp_path), codes.E101)


def test_empty_body_reports_e101(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "workflows/w/a.md": md("on: Incident\nagent: Investigator", "")},
    )
    assert_code(compile_project(tmp_path), codes.E101)


def test_orphan_step_reports_e103(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator"),
            "workflows/w/orphan.md": md("agent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E103)


def test_on_and_after_reports_e104(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/bad.md": md("on: Incident\nafter: helper\nagent: Investigator"),
            "workflows/w/helper.md": md("agent: Investigator"),  # orphan (E103 noise) — ok
        },
    )
    assert_code(compile_project(tmp_path), codes.E104)


def test_missing_after_target_reports_e105(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator"),
            "workflows/w/b.md": md("after: ghost\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E105)


def test_after_cycle_reports_e106(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/entry.md": md("on: Incident\nagent: Investigator"),
            "workflows/w/x.md": md("after: y\nagent: Investigator"),
            "workflows/w/y.md": md("after: x\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E106)


def test_two_entries_reports_e102(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator"),
            "workflows/w/b.md": md("on: WorkItem\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E102)


def test_duplicate_step_id_reports_e107(tmp_path):
    # Structurally rare via the filesystem; force it with a duplicated inventory entry.
    write_project(
        tmp_path,
        {"registry.yml": REGISTRY, "workflows/w/a.md": md("on: Incident\nagent: Investigator")},
    )
    path = tmp_path / "workflows" / "w" / "a.md"
    inv = Inventory(root=tmp_path, workflow_files=[path, path])
    diags = DiagnosticCollector()
    load_workflows(inv, Registry(), diags)
    assert codes.E107 in [d.code for d in diags.items]


def test_cron_quoted_parses(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md(
                'on: cron("1,15 * * * *", tz=America/New_York)\nagent: Investigator'
            ),
        },
    )
    result = compile_project(tmp_path)
    assert_no_m2_errors(result)
    step = result.project.workflows["upkeep"].steps["scan"]
    assert step.trigger.kind == "cron"
    assert step.trigger.expr == "1,15 * * * *"
    assert step.trigger.tz == "America/New_York"


def test_cron_unquoted_reports_e110(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md("on: cron(0 3 * * *)\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E110)


def test_cron_bad_expr_reports_e110(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md('on: cron("not a cron")\nagent: Investigator'),
        },
    )
    assert_code(compile_project(tmp_path), codes.E110)


def test_cron_bad_tz_reports_e110(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md(
                'on: cron("0 3 * * *", tz=Mars/Phobos)\nagent: Investigator'
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E110)


def test_event_trigger_filter_parses(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md(
                'on: Incident(issue_id="42")\nagent: Investigator'
            ),
        },
    )
    result = compile_project(tmp_path)
    assert_no_m2_errors(result)
    step = result.project.workflows["triage"].steps["investigate"]
    assert step.trigger.kind == "event"
    assert step.trigger.event == "Incident"
    assert step.trigger.filters == {"issue_id": "42"}


def test_event_trigger_multiple_filters_and_quoted_commas_parse(tmp_path):
    # Values are quoted, so a comma inside one must not split the arg list.
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY + "  Labeled: { repo: str, title: str }\n",
            "workflows/triage/investigate.md": md(
                'on: Labeled(repo="octocat/Hello-World", title="a, b")\nagent: Investigator'
            ),
        },
    )
    result = compile_project(tmp_path)
    assert_no_m2_errors(result)
    step = result.project.workflows["triage"].steps["investigate"]
    assert step.trigger.filters == {"repo": "octocat/Hello-World", "title": "a, b"}


def test_event_trigger_unquoted_filter_reports_e113(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md("on: Incident(issue_id=42)\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E113)


def test_event_trigger_empty_filter_list_reports_e113(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md("on: Incident()\nagent: Investigator"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E113)


def test_event_trigger_duplicate_filter_field_reports_e113(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md(
                'on: Incident(issue_id="1", issue_id="2")\nagent: Investigator'
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E113)
