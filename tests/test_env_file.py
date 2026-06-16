"""env_file addendum: Sandbox references an env file by path; compiler never reads it."""

from __future__ import annotations

from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from tests.helpers import write_project


def test_env_file_scalar_normalizes_to_list(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": "sandboxes:\n  default:\n    env_file: secrets/x.env\n"},
    )
    sb = compile_project(tmp_path).project.registry.sandboxes["default"]
    assert sb.env_file == ["secrets/x.env"]


def test_env_file_list_preserved(tmp_path):
    registry = "sandboxes:\n  default:\n    env_file: [a.env, b.env]\n"
    write_project(tmp_path, {"registry.yml": registry})
    sb = compile_project(tmp_path).project.registry.sandboxes["default"]
    assert sb.env_file == ["a.env", "b.env"]


def test_env_file_absent_is_empty(tmp_path):
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  default:\n    provider: local\n"})
    sb = compile_project(tmp_path).project.registry.sandboxes["default"]
    assert sb.env_file == []


def test_env_file_in_manifest(tmp_path):
    write_project(
        tmp_path,
        {"registry.yml": "sandboxes:\n  default:\n    env_file: secrets/x.env\n"},
    )
    manifest = to_manifest(compile_project(tmp_path).project)
    assert manifest["registry"]["sandboxes"]["default"]["env_file"] == ["secrets/x.env"]


def test_compile_clean_when_env_file_missing_on_disk(tmp_path):
    # The referenced file does NOT exist — compile must not read it, so no diagnostic.
    write_project(
        tmp_path,
        {"registry.yml": "sandboxes:\n  default:\n    env_file: secrets/does-not-exist.env\n"},
    )
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    assert not (tmp_path / "secrets").exists()  # we never created or touched it
