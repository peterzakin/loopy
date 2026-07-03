"""Every cookbook example doesn't just compile, it *runs*: fire the entry trigger and drive
the cascade to completion on the offline StubAgentHarness (no model key, no sandbox, no
network). This catches runtime-wiring breakage the compile check can't see, e.g. an entry
step that never fires, a step that doesn't execute, or a declared `emits` that never lands.

Coverage is explicit (one row per example) because each entry trigger differs: cron ticks
fire via `runtime.tick`, event and built-in triggers via `runtime.trigger`. The compile-only
guard in `test_examples_compile.py` covers discovery; this asserts execution.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loopy_core.compile.manifest import to_manifest
from loopy_core.compile.pipeline import compile_project
from loopy_runtime.bus.inproc import InProcessEventBus
from loopy_runtime.contract import Event
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.runtime.inmemory import InMemoryRuntime
from loopy_runtime.sandbox.local import LocalSandboxProvider
from loopy_runtime.secrets import StaticSecretsResolver
from tests.stub_harness import StubAgentHarness

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# name → (event_name, fields) for event/built-in triggered examples. Cron examples are
# handled separately (they fire via runtime.tick, keyed by the entry step id).
_URL = "https://example.com/x"
EVENT_CASES = {
    "issue-triage": (
        "Github.IssueOpened",
        {"number": 1, "repo": "me/app", "title": "t", "body": "b", "author": "u", "url": _URL},
        "issue-triage/triage",
        "IssueTriaged",
    ),
    "changelog": (
        "Github.PullRequestMerged",
        {"number": 2, "repo": "me/app", "title": "t", "url": _URL, "merged_by": "u"},
        "changelog/entry",
        "ChangelogUpdated",
    ),
    "uptime": (
        "Incident",
        {"url": _URL, "status": 503},
        "respond/open-issue",
        "Acknowledged",
    ),
}

# name → (expected step id, expected emitted event) for cron-triggered examples.
CRON_CASES = {
    "standup": ("standup/digest", "DigestReady"),
    "dep-upkeep": ("dep-upkeep/bump", "DepsBumped"),
}


def _runtime(example: str) -> InMemoryRuntime:
    result = compile_project(EXAMPLES / example)
    assert result.diagnostics.errors == [], [d.render() for d in result.diagnostics.errors]
    # round-trip through the manifest exactly as `loopy compile` + the runtime loader would
    m = Manifest.model_validate(json.loads(json.dumps(to_manifest(result.project))))
    return InMemoryRuntime(
        m,
        harness=StubAgentHarness(m.registry.events),
        sandboxes=LocalSandboxProvider(),
        secrets=StaticSecretsResolver({}),
        bus=InProcessEventBus(),
    )


@pytest.mark.parametrize("example", list(EVENT_CASES), ids=list(EVENT_CASES))
def test_event_example_runs(example: str):
    event_name, fields, step_id, emitted = EVENT_CASES[example]
    rt = _runtime(example)
    event = Event(name=event_name, fields=fields, id="trigger", emitted_at=datetime.now(UTC))
    run_id = asyncio.run(rt.trigger(event))
    assert run_id is not None, f"{example}: {event_name} started no run"
    assert rt.execution_log == [step_id], rt.execution_log
    assert emitted in rt.emitted_log, rt.emitted_log


@pytest.mark.parametrize("example", list(CRON_CASES), ids=list(CRON_CASES))
def test_cron_example_runs(example: str):
    step_id, emitted = CRON_CASES[example]
    rt = _runtime(example)
    run_id = asyncio.run(rt.tick(step_id, datetime.now(UTC)))
    assert run_id is not None, f"{example}: cron tick for {step_id} started no run"
    assert rt.execution_log == [step_id], rt.execution_log
    assert emitted in rt.emitted_log, rt.emitted_log
