"""Sandbox env namespacing: the prefix rule and the runtime passthrough resolution.

The compiler validates a sandbox's `env:` allow-list (see test_env_file.py); here we cover
what the runtime does with it — pulling each declared key from the sandbox's `<PREFIX>_<KEY>`
namespace in the engine environment, stripping the prefix, and letting it win over env_file.
"""

from __future__ import annotations

from loopy_core.registry.sandbox_env import sandbox_env_prefix
from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.secrets import EnvFileSecretsResolver


def test_prefix_normalizes_name():
    assert sandbox_env_prefix("BaseSandbox") == "BASESANDBOX"
    assert sandbox_env_prefix("code-fixer") == "CODE_FIXER"
    assert sandbox_env_prefix("default") == "DEFAULT"


def test_passthrough_pulls_declared_key_from_namespace(tmp_path):
    resolver = EnvFileSecretsResolver(tmp_path, environ={"SCRAPER_ANTHROPIC_API_KEY": "sk-ns"})
    spec = SandboxSpec(env=["ANTHROPIC_API_KEY"])
    assert resolver.resolve("Scraper", spec) == {"ANTHROPIC_API_KEY": "sk-ns"}


def test_passthrough_ignores_undeclared_and_bare_keys(tmp_path):
    # Only declared keys cross, and only via their namespaced form — a bare control-plane var
    # sharing the engine env never leaks in.
    environ = {
        "DAYTONA_API_KEY": "dt-secret",  # bare control-plane var, not namespaced
        "SCRAPER_OPENAI_API_KEY": "sk-undeclared",  # namespaced but not in env:
        "SCRAPER_ANTHROPIC_API_KEY": "sk-ns",
    }
    resolver = EnvFileSecretsResolver(tmp_path, environ=environ)
    spec = SandboxSpec(env=["ANTHROPIC_API_KEY"])
    assert resolver.resolve("Scraper", spec) == {"ANTHROPIC_API_KEY": "sk-ns"}


def test_namespaced_value_overrides_env_file(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "base.env").write_text("ANTHROPIC_API_KEY=sk-file\n")
    resolver = EnvFileSecretsResolver(tmp_path, environ={"SCRAPER_ANTHROPIC_API_KEY": "sk-ns"})
    spec = SandboxSpec(env_file=["secrets/base.env"], env=["ANTHROPIC_API_KEY"])
    # env_file provides the dev value; the platform-namespaced var wins.
    assert resolver.resolve("Scraper", spec)["ANTHROPIC_API_KEY"] == "sk-ns"


def test_env_file_value_used_when_no_namespaced_var(tmp_path):
    # The dev path: env_file supplies the key, nothing namespaced in the environment.
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "base.env").write_text("ANTHROPIC_API_KEY=sk-file\n")
    resolver = EnvFileSecretsResolver(tmp_path, environ={})
    spec = SandboxSpec(env_file=["secrets/base.env"], env=["ANTHROPIC_API_KEY"])
    assert resolver.resolve("Scraper", spec)["ANTHROPIC_API_KEY"] == "sk-file"


def test_declared_but_absent_key_is_not_injected(tmp_path):
    # Declared in env: but present in neither source — simply omitted (the harness's own
    # required-key check reports a genuinely missing credential downstream).
    resolver = EnvFileSecretsResolver(tmp_path, environ={})
    spec = SandboxSpec(env=["ANTHROPIC_API_KEY"])
    assert resolver.resolve("Scraper", spec) == {}


def test_two_sandboxes_get_different_values_for_same_key(tmp_path):
    environ = {
        "SCRAPER_ANTHROPIC_API_KEY": "sk-scraper",
        "REVIEWER_ANTHROPIC_API_KEY": "sk-reviewer",
    }
    resolver = EnvFileSecretsResolver(tmp_path, environ=environ)
    spec = SandboxSpec(env=["ANTHROPIC_API_KEY"])
    assert resolver.resolve("Scraper", spec)["ANTHROPIC_API_KEY"] == "sk-scraper"
    assert resolver.resolve("Reviewer", spec)["ANTHROPIC_API_KEY"] == "sk-reviewer"
