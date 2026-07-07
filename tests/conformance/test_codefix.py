"""Smoke test for the `codefix` example (TODO #14): compile the example fresh and drive
one `CodeTask` event end-to-end through the runtime.

This is the always-on, no-creds half of #14's "one-command CI smoke test" — it runs the
same `compile` + `trigger` path a real local run uses (catching the #5–#8 class of breakage
mechanically), but on the offline StubAgentHarness so it needs no model key, no GitHub token,
and no network. The live one-command variant (a real edit against a throwaway repo) is
`tests/fixtures/codefix/smoke.sh`, documented in the fixture README.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import load_manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness

EXAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "codefix"


def _manifest(tmp_path: Path):
    """Compile the committed example to a manifest, exactly as `loopy compile` would."""
    result = compile_project(EXAMPLE)
    assert result.diagnostics.items == [], [d.render() for d in result.diagnostics.items]
    out = tmp_path / "manifest.json"
    out.write_text(json.dumps(to_manifest(result.project)))
    return load_manifest(out)


def _runtime(tmp_path: Path) -> InMemoryRuntime:
    m = _manifest(tmp_path)
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


def _code_task() -> Event:
    return Event(
        name="CodeTask",
        fields={"task": "add a hello-world note to the README", "branch": "codefix/hello"},
        id="trigger",
        emitted_at=datetime.now(UTC),
    )


def test_codetask_runs_the_open_pr_step(tmp_path: Path):
    rt = _runtime(tmp_path)
    run_id = asyncio.run(rt.trigger(_code_task()))
    assert run_id is not None
    assert rt.execution_log == ["codefix/open-pr"]
    assert rt.emitted_log == ["PROpened"]


def test_open_pr_step_produces_a_pr_url(tmp_path: Path):
    rt = _runtime(tmp_path)
    run_id = asyncio.run(rt.trigger(_code_task()))
    outputs = asyncio.run(rt.state.outputs(run_id))
    assert "pr_url" in outputs["open-pr"].fields
    assert "summary" in outputs["open-pr"].fields


def test_run_ids_are_unique_across_processes(tmp_path: Path):
    # Each `loopy trigger` is a fresh runtime/process. Two identical tasks must NOT collide on
    # the same run id (the bug behind two tasks landing on one branch); a per-process counter
    # would restart at 1 for both.
    id1 = asyncio.run(_runtime(tmp_path).trigger(_code_task()))
    id2 = asyncio.run(_runtime(tmp_path).trigger(_code_task()))
    assert id1 != id2
    assert id1.startswith("codefix-") and id2.startswith("codefix-")


def test_unsubscribed_event_starts_no_run(tmp_path: Path):
    rt = _runtime(tmp_path)
    run_id = asyncio.run(
        rt.trigger(Event(name="Nope", fields={}, id="x", emitted_at=datetime.now(UTC)))
    )
    assert run_id is None
