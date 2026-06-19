"""B8 StateStore conformance: the same suite against every StateStore implementation, so the
in-memory and SQLite backends stay interchangeable behind the Protocol. Plus SQLite-specific
checks (durability across reopen, read-only mode).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from loopy_runtime.contract import Event, RunEvent, StepOutput
from loopy_runtime.state.inmemory import InMemoryStateStore
from loopy_runtime.state.sqlite import SqliteStateStore


def _event(name: str = "Ping") -> Event:
    return Event(name=name, fields={}, id="evt", emitted_at=datetime.now(UTC))


def _at(secs: int = 0) -> datetime:
    return datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=secs)


@pytest.fixture(params=["inmemory", "sqlite"])
def store(request, tmp_path):
    if request.param == "inmemory":
        return InMemoryStateStore()
    return SqliteStateStore(tmp_path / "state.db")


# ── per-run history / outputs ────────────────────────────────────────────────────
def test_history_records_in_append_order(store):
    async def go():
        await store.create_run("wf-1", "1", _event())
        await store.append("wf-1", RunEvent("run_started", None, {"event": "Ping"}, _at(0)))
        await store.append("wf-1", RunEvent("step_completed", "wf/a", {}, _at(1)))
        await store.append("wf-1", RunEvent("run_completed", None, {}, _at(2)))
        return [e.kind for e in await store.history("wf-1")]

    assert asyncio.run(go()) == ["run_started", "step_completed", "run_completed"]


def test_outputs_roundtrip(store):
    async def go():
        await store.create_run("wf-1", "1", _event())
        await store.record_output("wf-1", "wf/a", StepOutput({"url": "https://x", "n": 3}))
        return await store.outputs("wf-1")

    out = asyncio.run(go())
    assert out["wf/a"].fields == {"url": "https://x", "n": 3}


def test_watermark_and_dedupe(store):
    async def go():
        before = await store.get_watermark("poll/x")
        await store.set_watermark("poll/x", _at(5))
        after = await store.get_watermark("poll/x")
        first_seen = await store.seen("k1")
        await store.mark_seen("k1")
        return before, after, first_seen, await store.seen("k1")

    before, after, first_seen, now_seen = asyncio.run(go())
    assert before is None and after == _at(5)
    assert first_seen is False and now_seen is True


# ── list_runs ────────────────────────────────────────────────────────────────────
def test_list_runs_newest_first_with_derived_state(store):
    async def go():
        await store.create_run("wf-1", "1", _event("A"))
        await store.append("wf-1", RunEvent("run_completed", None, {}, _at(1)))
        await store.create_run("wf-2", "1", _event("B"))
        await store.append("wf-2", RunEvent("run_failed", "wf/a", {"error": "boom"}, _at(2)))
        await store.create_run("wf-3", "1", _event("C"))  # still running
        return await store.list_runs()

    runs = asyncio.run(go())
    assert [r.run_id for r in runs] == ["wf-3", "wf-2", "wf-1"]  # newest first
    by_id = {r.run_id: r for r in runs}
    assert by_id["wf-1"].state == "completed"
    assert by_id["wf-2"].state == "failed" and by_id["wf-2"].error == "boom"
    assert by_id["wf-3"].state == "running" and by_id["wf-3"].ended_at is None
    assert by_id["wf-3"].workflow == "wf" and by_id["wf-3"].entry_event == "C"


def test_list_runs_filter_and_paginate(store):
    async def go():
        for i in range(1, 6):
            await store.create_run(f"wf-{i}", "1", _event())
            if i % 2 == 0:
                await store.append(f"wf-{i}", RunEvent("run_failed", None, {"error": "e"}, _at(i)))
        failed = await store.list_runs(state="failed")
        page = await store.list_runs(limit=2, offset=1)
        return failed, page

    failed, page = asyncio.run(go())
    assert {r.run_id for r in failed} == {"wf-2", "wf-4"}
    assert [r.run_id for r in page] == ["wf-4", "wf-3"]  # newest-first, skip wf-5, take 2


def test_workflow_with_hyphen_is_parsed(store):
    async def go():
        await store.create_run("my-cool-wf-7", "1", _event())
        return (await store.list_runs())[0]

    assert asyncio.run(go()).workflow == "my-cool-wf"


# ── SQLite-specific ───────────────────────────────────────────────────────────────
def test_sqlite_persists_across_reopen(tmp_path):
    db = tmp_path / "state.db"

    async def write():
        s = SqliteStateStore(db)
        await s.create_run("wf-1", "1", _event("A"))
        await s.append("wf-1", RunEvent("run_completed", None, {}, _at(1)))
        await s.record_output("wf-1", "wf/a", StepOutput({"ok": True}))
        s.close()

    async def read():
        s = SqliteStateStore(db, read_only=True)
        runs = await s.list_runs()
        outs = await s.outputs("wf-1")
        s.close()
        return runs, outs

    asyncio.run(write())
    runs, outs = asyncio.run(read())
    assert len(runs) == 1 and runs[0].state == "completed"
    assert outs["wf/a"].fields == {"ok": True}


def test_sqlite_read_only_rejects_writes(tmp_path):
    db = tmp_path / "state.db"
    SqliteStateStore(db).close()  # create the file/schema

    async def go():
        s = SqliteStateStore(db, read_only=True)
        with pytest.raises(Exception):  # noqa: B017 - sqlite raises OperationalError on RO write
            await s.create_run("wf-1", "1", _event())
        s.close()

    asyncio.run(go())


def test_runtime_persists_run_into_sqlite_for_the_dashboard(tmp_path):
    """End-to-end: a real cascade run through InMemoryRuntime lands in the SQLite store such
    that a separate read-only reader (the dashboard) sees it via list_runs + history."""
    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.manifest_model import Manifest
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.local import LocalSandboxProvider
    from loopy_runtime.secrets import StaticSecretsResolver
    from tests.stub_harness import StubAgentHarness

    db = tmp_path / "state.db"
    manifest = Manifest.model_validate(
        {
            "schema_version": "1",
            "registry": {"sandboxes": {}, "agents": {}, "events": {"Ping": {"fields": {}}}},
            "workflows": {
                "greet": {
                    "entry": "x",
                    "steps": {
                        "x": {
                            "id": "greet/x",
                            "trigger": {"kind": "event", "event": "Ping"},
                            "after": [],
                            "agent": None,
                            "output": {},
                            "emits": [],
                            "budget": None,
                            "body": "do",
                            "refs": [],
                        }
                    },
                }
            },
            "sensors": [],
            "lineage": {"events": {}},
        }
    )

    async def run_one():
        store = SqliteStateStore(db)
        rt = InMemoryRuntime(
            manifest,
            harness=StubAgentHarness(manifest.registry.events),
            sandboxes=LocalSandboxProvider(),
            secrets=StaticSecretsResolver({}),
            bus=InProcessEventBus(),
            state=store,
        )
        await rt.trigger(_event())
        store.close()

    asyncio.run(run_one())

    async def read_back():
        reader = SqliteStateStore(db, read_only=True)
        runs = await reader.list_runs()
        history = await reader.history(runs[0].run_id) if runs else []
        reader.close()
        return runs, history

    runs, history = asyncio.run(read_back())
    assert len(runs) == 1
    assert runs[0].workflow == "greet" and runs[0].state == "completed"
    assert runs[0].entry_event == "Ping"
    assert [e.kind for e in history] == ["run_started", "step_completed", "run_completed"]
