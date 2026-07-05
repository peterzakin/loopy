"""env_file addendum: Sandbox references an env file by path; compiler never reads it.

Also covers the `env:` passthrough allow-list (names the engine forwards from its own
environment into the sandbox): parsing, manifest serialization, and the two compile gates
(E216 bad/reserved entry, E217 namespace collision)."""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from tests.helpers import assert_code, write_project
from tests.helpers import codes as result_codes


def test_env_file_scalar_normalizes_to_list(tmp_path):
    write_project(
        tmp_path,
        {
            "registry.yml": "sandboxes:\n  default:\n    provider: local\n"
            "    env_file: secrets/x.env\n"
        },
    )
    sb = compile_project(tmp_path).project.registry.sandboxes["default"]
    assert sb.env_file == ["secrets/x.env"]


def test_env_file_list_preserved(tmp_path):
    registry = "sandboxes:\n  default:\n    provider: local\n    env_file: [a.env, b.env]\n"
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
        {
            "registry.yml": "sandboxes:\n  default:\n    provider: local\n"
            "    env_file: secrets/x.env\n"
        },
    )
    manifest = to_manifest(compile_project(tmp_path).project)
    assert manifest["registry"]["sandboxes"]["default"]["env_file"] == ["secrets/x.env"]


def test_compile_clean_when_env_file_missing_on_disk(tmp_path):
    # The referenced file does NOT exist — compile must not read it, so no diagnostic.
    write_project(
        tmp_path,
        {
            "registry.yml": "sandboxes:\n  default:\n    provider: local\n"
            "    env_file: secrets/does-not-exist.env\n"
        },
    )
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    assert not (tmp_path / "secrets").exists()  # we never created or touched it


# ── env: passthrough allow-list ─────────────────────────────────────────────────


def _sandbox_env(value: str) -> str:
    """A one-sandbox registry whose `default` declares `env: <value>`."""
    return f"sandboxes:\n  default:\n    provider: local\n    env: {value}\n"


def test_env_scalar_normalizes_to_list(tmp_path):
    write_project(tmp_path, {"registry.yml": _sandbox_env("ANTHROPIC_API_KEY")})
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    assert result.project.registry.sandboxes["default"].env == ["ANTHROPIC_API_KEY"]


def test_env_list_preserved_and_in_manifest(tmp_path):
    write_project(tmp_path, {"registry.yml": _sandbox_env("[ANTHROPIC_API_KEY, GITHUB_TOKEN]")})
    result = compile_project(tmp_path)
    assert result.diagnostics.items == []
    manifest = to_manifest(result.project)
    assert manifest["registry"]["sandboxes"]["default"]["env"] == [
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
    ]


def test_env_absent_is_empty(tmp_path):
    write_project(tmp_path, {"registry.yml": "sandboxes:\n  default:\n    provider: local\n"})
    assert compile_project(tmp_path).project.registry.sandboxes["default"].env == []


def test_env_reserved_control_plane_key_reports_e216(tmp_path):
    write_project(tmp_path, {"registry.yml": _sandbox_env("DAYTONA_API_KEY")})
    assert_code(compile_project(tmp_path), codes.E216, file="registry.yml")


def test_env_reserved_loopy_prefix_reports_e216(tmp_path):
    write_project(tmp_path, {"registry.yml": _sandbox_env("LOOPY_ADMIN_TOKEN")})
    assert_code(compile_project(tmp_path), codes.E216)


def test_env_wildcard_reports_e216(tmp_path):
    # A `*` is not a valid env-var name, so there is no forward-everything hatch.
    write_project(tmp_path, {"registry.yml": _sandbox_env('["*"]')})
    assert_code(compile_project(tmp_path), codes.E216)


def test_env_github_token_is_allowed(tmp_path):
    # GITHUB_TOKEN is a legitimate per-sandbox git credential, not a control-plane key.
    write_project(tmp_path, {"registry.yml": _sandbox_env("GITHUB_TOKEN")})
    assert codes.E216 not in result_codes(compile_project(tmp_path))


def test_env_namespace_collision_reports_e217(tmp_path):
    # `Web-app` and `Web_app` both normalize to the `WEB_APP` namespace; one uses env:.
    registry = (
        "sandboxes:\n"
        "  Web-app:\n    provider: local\n    env: ANTHROPIC_API_KEY\n"
        "  Web_app:\n    provider: local\n"
    )
    write_project(tmp_path, {"registry.yml": registry})
    assert_code(compile_project(tmp_path), codes.E217)


def test_env_namespace_collision_ignored_without_env(tmp_path):
    # Same prefix collision, but neither declares env: — no ambiguity, so no diagnostic.
    registry = "sandboxes:\n  Web-app:\n    provider: local\n  Web_app:\n    provider: local\n"
    write_project(tmp_path, {"registry.yml": registry})
    assert codes.E217 not in result_codes(compile_project(tmp_path))
