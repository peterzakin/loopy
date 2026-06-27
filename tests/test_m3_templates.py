"""M3: template ref extraction (E301) + static resolution (T1–T3, E302–E305)."""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.pipeline import compile_project
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes

REGISTRY = (
    "defaults: { agent: { sandbox: default } }\n"
    "sandboxes: { default: { provider: local } }\n"
    "agents: { Investigator: {} }\n"
    "events:\n"
    "  Incident: { fields: { issue_id: str, title: str } }\n"
)

M3_CODES = {codes.E301, codes.E302, codes.E303, codes.E304, codes.E305}


def md(frontmatter: str, body: str = "Do the work.") -> str:
    return f"---\n{frontmatter}\n---\n{body}\n"


def assert_no_m3_errors(result) -> None:
    fired = set(result_codes(result)) & M3_CODES
    assert not fired, f"unexpected M3 codes: {fired}"


def test_valid_event_ref_resolves_clean(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md(
                "on: Incident\nagent: Investigator", "Investigate {{ event.issue_id }} now."
            ),
        },
    )
    assert_no_m3_errors(compile_project(tmp_path))


def test_valid_step_ref_resolves_clean(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md(
                "on: Incident\nagent: Investigator\noutput:\n  verdict: enum[pass, fail]", "Decide."
            ),
            "workflows/w/b.md": md("after: a\nagent: Investigator", "Saw {{ a.verdict }}."),
        },
    )
    assert_no_m3_errors(compile_project(tmp_path))


def test_refs_recorded_on_step_with_accurate_line(tmp_path):
    # Body's ref is on file line 6 (---, on, agent, ---, "Line1", "Use {{...}}").
    body = "Line1 body\nUse {{ event.issue_id }} here"
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/triage/investigate.md": md("on: Incident\nagent: Investigator", body),
        },
    )
    step = compile_project(tmp_path).project.workflows["triage"].steps["investigate"]
    assert len(step.refs) == 1
    ref = step.refs[0]
    assert ref.producer == "event"
    assert ref.field == "issue_id"
    assert ref.span.line == 6


def test_control_flow_reports_e301(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator", "{% if x %}hi{% endif %}"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E301)


def test_dotted_path_reports_e301(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator", "{{ event.a.b }}"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E301)


def test_filter_reports_e301(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md(
                "on: Incident\nagent: Investigator", "{{ event.title | upper }}"
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E301)


def test_unknown_event_field_reports_e302(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator", "{{ event.ghost }}"),
        },
    )
    assert_code(compile_project(tmp_path), codes.E302)


def test_cron_builtin_fields_resolve_clean(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md(
                'on: cron("0 3 * * *")\nagent: Investigator',
                "Scan since {{ event.last_run }} at {{ event.scheduled_at }}.",
            ),
        },
    )
    assert_no_m3_errors(compile_project(tmp_path))


def test_cron_other_field_reports_e303(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/upkeep/scan.md": md(
                'on: cron("0 3 * * *")\nagent: Investigator', "{{ event.issue_id }}"
            ),
        },
    )
    assert_code(compile_project(tmp_path), codes.E303)


def test_non_predecessor_step_ref_reports_e304(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md("on: Incident\nagent: Investigator", "Decide."),
            "workflows/w/b.md": md("after: a\nagent: Investigator", "Use {{ c.x }}."),
        },
    )
    assert_code(compile_project(tmp_path), codes.E304)


def test_unknown_predecessor_field_reports_e305(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": REGISTRY,
            "workflows/w/a.md": md(
                "on: Incident\nagent: Investigator\noutput:\n  verdict: str", "Decide."
            ),
            "workflows/w/b.md": md("after: a\nagent: Investigator", "Use {{ a.ghost }}."),
        },
    )
    assert_code(compile_project(tmp_path), codes.E305)
