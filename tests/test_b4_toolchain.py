"""B4 / TODO #16 — the harness toolchain contract.

Three layers, all offline:
  * `ToolchainLayer.merge` + `compose_image` (pure): how a harness's additive toolchain
    composes onto a (harness-agnostic) user `image:` spec.
  * Each harness declares its CLI + runtime and the binaries to probe.
  * The runner's post-acquire probe (`_verify_toolchain`) fails fast when the live sandbox
    is missing a required tool.
"""

from __future__ import annotations

import asyncio

import pytest

from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import ExecResult, ToolchainLayer
from loopy_runtime.harness.claude_code import ClaudeCodeHarness
from loopy_runtime.harness.codex import CodexHarness
from loopy_runtime.harness.router import HarnessRouter
from loopy_runtime.manifest_model import AgentSpec, HarnessSpec, Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.sandbox.toolchain import compose_image
from loopy_runtime.secrets import StaticSecretsResolver

# --- ToolchainLayer.merge (pure) ---------------------------------------------


def test_merge_concatenates_dedup_and_env_later_wins():
    a = ToolchainLayer(apt=("git", "ca-certificates"), probe=("git",), env={"X": "1", "Y": "1"})
    b = ToolchainLayer(
        apt=("git", "nodejs"), run=("npm i -g x",), probe=("claude",), env={"Y": "2"}
    )
    merged = a.merge(b)
    assert merged.apt == ("git", "ca-certificates", "nodejs")  # de-duped, order preserved
    assert merged.run == ("npm i -g x",)
    assert set(merged.probe) == {"git", "claude"}
    assert merged.env == {"X": "1", "Y": "2"}  # b wins on conflict


# --- compose_image (pure) ----------------------------------------------------


def test_compose_prepends_layers_ahead_of_user():
    layer = ToolchainLayer(apt=("nodejs",), run=("npm i -g claude",))
    image = compose_image({"apt": ["ripgrep"], "run": ["make build"]}, layer)
    assert image["apt"] == ["nodejs", "ripgrep"]  # toolchain installs first
    assert image["run"] == ["npm i -g claude", "make build"]


def test_compose_normalizes_str_and_none():
    layer = ToolchainLayer(apt=("git",))
    assert compose_image("node:20-slim", layer) == {"base": "node:20-slim", "apt": ["git"]}
    assert compose_image(None, layer) == {"apt": ["git"]}


def test_compose_user_env_wins():
    layer = ToolchainLayer(env={"PATH": "/tool/bin", "TOOL": "1"})
    image = compose_image({"env": {"PATH": "/user/bin"}}, layer)
    assert image["env"] == {"PATH": "/user/bin", "TOOL": "1"}


def test_compose_skips_snapshot_images():
    # A snapshot is prebuilt and can't take build layers — composition is a no-op; the
    # runtime probe is the backstop that the snapshot actually bundles the toolchain.
    layer = ToolchainLayer(apt=("nodejs",), run=("npm i -g claude",))
    assert compose_image({"snapshot": "org/snap:1"}, layer) == {"snapshot": "org/snap:1"}


def test_compose_empty_layer_is_noop():
    assert compose_image({"base": "python:3.12-slim"}, ToolchainLayer()) == {
        "base": "python:3.12-slim"
    }


# --- harness toolchains ------------------------------------------------------

_CLAUDE = AgentSpec(harness=HarnessSpec(runtime="claude-code", model="claude-sonnet-4-6"))
_CODEX = AgentSpec(harness=HarnessSpec(runtime="codex", model="gpt-5-codex"))


def test_claude_toolchain_has_substrate_plus_cli():
    layer = ClaudeCodeHarness({"a": _CLAUDE}).toolchain(_CLAUDE)
    assert "git" in layer.apt and "nodejs" in layer.apt  # substrate + runtime
    assert any("claude-code" in c for c in layer.run)
    assert layer.probe == ("git", "claude")


def test_codex_toolchain_has_substrate_plus_cli():
    layer = CodexHarness({"a": _CODEX}).toolchain(_CODEX)
    assert "git" in layer.apt and "nodejs" in layer.apt
    assert any("codex" in c for c in layer.run)
    assert set(layer.probe) == {"git", "codex"}


def test_required_tools_is_the_probe():
    assert ClaudeCodeHarness({"a": _CLAUDE}).required_tools(_CLAUDE) == {"git", "claude"}


def test_router_delegates_toolchain_to_the_right_harness():
    router = HarnessRouter({"c": _CLAUDE, "x": _CODEX})
    assert router.required_tools(_CLAUDE) == {"git", "claude"}
    assert router.required_tools(_CODEX) == {"git", "codex"}


# --- runtime probe (_verify_toolchain) ---------------------------------------


class _ProbeSandbox:
    """Returns a canned probe result; `missing` are the tool names it reports absent."""

    id = "probe"

    def __init__(self, missing=(), raises=False):
        self._missing = missing
        self._raises = raises

    async def exec(self, cmd):
        if self._raises:
            raise FileNotFoundError("sh")  # e.g. bare local provider with an empty env
        return ExecResult(0, "\n".join(self._missing), "")

    async def release(self):
        return None


class _ToolHarness:
    def __init__(self, tools):
        self._tools = set(tools)

    def required_tools(self, agent):
        return set(self._tools)


def _empty_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {}},
            "workflows": {},
            "sensors": [],
            "lineage": {"events": {}},
        }
    )


def _runtime(tools) -> InMemoryRuntime:
    return InMemoryRuntime(
        _empty_manifest(),
        harness=_ToolHarness(tools),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


def test_probe_passes_when_all_tools_present():
    rt = _runtime({"git", "claude"})
    asyncio.run(rt.runner._verify_toolchain(_ProbeSandbox(missing=()), _CLAUDE, "fixer", "default"))


def test_probe_raises_listing_missing_tools():
    rt = _runtime({"git", "claude"})
    with pytest.raises(RuntimeError, match="missing tool.*claude"):
        asyncio.run(
            rt.runner._verify_toolchain(_ProbeSandbox(missing=("claude",)), _CLAUDE, "f", "default")
        )


def test_probe_failure_treats_all_tools_as_missing():
    rt = _runtime({"git", "claude"})
    with pytest.raises(RuntimeError, match="missing tool"):
        asyncio.run(
            rt.runner._verify_toolchain(_ProbeSandbox(raises=True), _CLAUDE, "f", "default")
        )


def test_probe_skipped_when_no_required_tools():
    rt = _runtime(set())
    # No exec needed; a sandbox that would raise on exec must never be called.
    asyncio.run(rt.runner._verify_toolchain(_ProbeSandbox(raises=True), _CLAUDE, "f", "default"))
