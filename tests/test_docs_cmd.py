"""`loopy docs` — the docs that ship inside the package.

The command exists for coding agents (and offline humans): `loopy docs` must print the
full authoring reference as plain markdown, and `loopy docs errors` must render the live
diagnostic catalog so it can never drift from the compiler's own table.
"""

from __future__ import annotations

from typer.testing import CliRunner

from loopy_cli import app
from loopy_core.compile.codes import ALL_CODES, DESCRIPTIONS

runner = CliRunner()


def test_docs_default_prints_authoring_reference():
    result = runner.invoke(app, ["docs"])
    assert result.exit_code == 0, result.output
    # The load-bearing sections an agent needs: the verify loop, the outputs/events
    # distinction, the field-type table, and the secrets rule.
    assert "loopy compile --check ." in result.output
    assert "Outputs and events" in result.output
    assert "enum[a, b, c]" in result.output
    assert "inherits nothing from your shell" in result.output


def test_docs_deployment_prints_the_serve_contract():
    result = runner.invoke(app, ["docs", "deployment"])
    assert result.exit_code == 0, result.output
    # The load-bearing pieces: the env-var contract, rotation, and the fail-closed rule.
    assert "LOOPY_ADMIN_TOKEN" in result.output
    assert "LOOPY_ADMIN_TOKEN_NEXT" in result.output
    assert "Fail-closed" in result.output
    assert "$PORT" in result.output


def test_docs_errors_renders_every_code():
    result = runner.invoke(app, ["docs", "errors"])
    assert result.exit_code == 0, result.output
    for code in ALL_CODES:
        assert code in result.output, f"{code} missing from `loopy docs errors`"


def test_docs_errors_is_the_compilers_own_table():
    """Every code has a description and the derived ALL_CODES stays the frozen contract —
    a new code can't ship without a catalog line."""
    assert ALL_CODES == frozenset(DESCRIPTIONS)
    assert all(DESCRIPTIONS[code].strip() for code in DESCRIPTIONS)


def test_docs_unknown_topic_errors_with_topic_list():
    result = runner.invoke(app, ["docs", "nope"])
    assert result.exit_code == 1
    assert "unknown docs topic" in result.output
    assert "authoring" in result.output
    assert "errors" in result.output
