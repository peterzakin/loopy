"""Dashboard read API (B12): pure view builders + the FastAPI route handlers.

No HTTP server / httpx — the route handlers are awaited directly (same approach as the sensor
tests), and the view builders are tested as plain functions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from loopy_runtime.contract import Event, RunEvent, StepOutput
from loopy_runtime.dashboard.app import create_app
from loopy_runtime.dashboard.views import build_run_detail, summary_to_dict
from loopy_runtime.state.inmemory import InMemoryStateStore


def _at(secs: int = 0) -> datetime:
    return datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=secs)


def _event(name: str = "Ping") -> Event:
    return Event(name=name, fields={}, id="e", emitted_at=_at(0))


def _endpoint(app, path: str):
    """The handler coroutine FastAPI registered for `path` (invoked directly, no server)."""
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)


# ── pure builders ─────────────────────────────────────────────────────────────────
def test_build_run_detail_derives_status_steps_and_emits():
    history = [
        RunEvent("run_started", None, {"event": "Ping"}, _at(0)),
        RunEvent("step_completed", "wf/a", {}, _at(1)),
        RunEvent("event_emitted", "wf/a", {"event": "WorkItem"}, _at(1)),
        RunEvent("run_completed", None, {}, _at(2)),
    ]
    outputs = {"wf/a": StepOutput({"url": "https://x"})}
    detail = build_run_detail("wf-1", history, outputs)
    assert detail["state"] == "completed"
    assert detail["workflow"] == "wf" and detail["entry_event"] == "Ping"
    assert detail["created_at"] == _at(0).isoformat()
    assert detail["steps"] == {"wf/a": "completed"}
    assert detail["emitted"] == ["WorkItem"]
    assert detail["outputs"] == {"wf/a": {"url": "https://x"}}
    assert [e["kind"] for e in detail["history"]] == [
        "run_started", "step_completed", "event_emitted", "run_completed"
    ]


def test_build_run_detail_marks_failed_step():
    history = [
        RunEvent("run_started", None, {"event": "Ping"}, _at(0)),
        RunEvent("run_failed", "wf/a", {"error": "boom"}, _at(1)),
    ]
    detail = build_run_detail("wf-1", history, {})
    assert detail["state"] == "failed" and detail["error"] == "boom"
    assert detail["steps"] == {"wf/a": "failed"}
    assert detail["ended_at"] == _at(1).isoformat()


# ── route handlers (awaited directly) ───────────────────────────────────────────────
def _seed() -> InMemoryStateStore:
    store = InMemoryStateStore()

    async def go():
        await store.create_run("wf-1", "1", _event("A"))
        await store.append("wf-1", RunEvent("run_started", None, {"event": "A"}, _at(0)))
        await store.append("wf-1", RunEvent("run_completed", None, {}, _at(1)))
        await store.create_run("wf-2", "1", _event("B"))
        await store.append("wf-2", RunEvent("run_started", None, {"event": "B"}, _at(2)))
        await store.append("wf-2", RunEvent("run_failed", "wf/x", {"error": "boom"}, _at(3)))

    asyncio.run(go())
    return store


def test_list_runs_endpoint_returns_newest_first():
    app = create_app(_seed())
    rows = asyncio.run(_endpoint(app, "/api/runs")())
    assert [r["run_id"] for r in rows] == ["wf-2", "wf-1"]
    assert rows[0]["state"] == "failed" and rows[0]["error"] == "boom"


def test_list_runs_endpoint_filters_by_state():
    app = create_app(_seed())
    rows = asyncio.run(_endpoint(app, "/api/runs")(state="completed"))
    assert [r["run_id"] for r in rows] == ["wf-1"]


def test_list_runs_endpoint_rejects_bad_state():
    app = create_app(_seed())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint(app, "/api/runs")(state="bogus"))
    assert exc.value.status_code == 400


def test_get_run_endpoint_returns_detail():
    app = create_app(_seed())
    detail = asyncio.run(_endpoint(app, "/api/runs/{run_id}")(run_id="wf-2"))
    assert detail["state"] == "failed"
    assert [e["kind"] for e in detail["history"]] == ["run_started", "run_failed"]


def test_get_run_endpoint_404_for_unknown():
    app = create_app(_seed())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint(app, "/api/runs/{run_id}")(run_id="nope-9"))
    assert exc.value.status_code == 404


def test_routes_registered():
    app = create_app(InMemoryStateStore())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/api/runs", "/api/runs/{run_id}"} <= paths


def test_index_serves_the_static_html():
    from pathlib import Path

    app = create_app(InMemoryStateStore())
    resp = asyncio.run(_endpoint(app, "/")())
    # FileResponse pointing at the bundled single-page app.
    assert str(resp.path).endswith("static/index.html")
    assert Path(resp.path).is_file()


def test_static_assets_present():
    from loopy_runtime.dashboard.app import _STATIC

    for name in ("index.html", "app.js", "style.css"):
        assert (_STATIC / name).is_file()


# ── loopy admin CLI ─────────────────────────────────────────────────────────────────
def test_admin_command_help():
    from typer.testing import CliRunner

    from loopy_cli import app

    result = CliRunner().invoke(app, ["admin", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.stdout and "--port" in result.stdout


def test_admin_errors_clearly_when_db_missing(tmp_path):
    from typer.testing import CliRunner

    from loopy_cli import app

    result = CliRunner().invoke(app, ["admin", str(tmp_path / "nope.db")])
    assert result.exit_code == 1
    # the friendly read-only-missing message, not a stack trace
    assert "no state DB" in result.output


def test_summary_to_dict_shape():
    store = _seed()
    rows = asyncio.run(store.list_runs())
    d = summary_to_dict(rows[0])
    assert set(d) == {
        "run_id", "workflow", "state", "entry_event", "created_at", "ended_at", "error"
    }
