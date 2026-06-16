"""M0 acceptance: code registry, no-op pipeline, diagnostics rendering + exit codes."""

from __future__ import annotations

from loopy_core.compile.codes import ALL_CODES
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.compile.pipeline import compile_project
from loopy_core.span import Span


def test_all_section14_codes_present_no_phantoms():
    # 26 codes total; E108/E109 intentionally do not exist.
    assert len(ALL_CODES) == 26
    assert "LOOPY-E108" not in ALL_CODES
    assert "LOOPY-E109" not in ALL_CODES


def test_pipeline_builds_a_project():
    # The pipeline always returns a Project (possibly with diagnostics).
    result = compile_project(".")
    assert result.project is not None


def test_diagnostic_renders_with_file_line():
    diags = DiagnosticCollector()
    diags.error("LOOPY-E001", "registry unreadable", span=Span(file="registry.yml", line=3))
    rendered = diags.items[0].render()
    assert "LOOPY-E001" in rendered
    assert "registry.yml:3" in rendered


def test_errors_fail_build_warnings_do_not():
    diags = DiagnosticCollector()
    diags.warning("LOOPY-W501", "dead trigger")
    assert diags.exit_code() == 0
    assert not diags.has_errors()

    diags.error("LOOPY-E001", "boom")
    assert diags.exit_code() == 1
    assert diags.has_errors()
