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
from loopy_runtime.dashboard.views import (
    build_meta,
    build_registry,
    build_run_detail,
    build_sensors,
    build_workflows,
    summary_to_dict,
)
from loopy_runtime.manifest_model import Manifest
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
        "run_started",
        "step_completed",
        "event_emitted",
        "run_completed",
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

    for name in ("index.html", "app.js", "style.css", "loopy-ui.css"):
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


# ── manifest views (templates / registry / schedules) ───────────────────────────────
def _manifest() -> Manifest:
    """A small but representative manifest: one event-triggered workflow with a 2-step DAG, one
    cron workflow, an agent, a sandbox with a secret env_file, a poll sensor and a webhook."""
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "compiled_at": "2026-06-19T00:00:00Z",
            "loopy_version": "0.1.0",
            "registry": {
                "agents": {
                    "Fixer": {
                        "harness": {"runtime": "claude-code", "model": "claude-opus-4-8"},
                        "sandbox": "default",
                        "skills": ["testing"],
                    }
                },
                "sandboxes": {
                    "default": {
                        "provider": "daytona",
                        "image": {"debian_slim": "3.12"},
                        "network": ["github.com"],
                        "env_file": ["secrets/default.env", "secrets/extra.env"],
                        "repos": [{"url": "acme/runbooks", "depth": 1}],
                    }
                },
                "events": {
                    "WorkItem": {"fields": {"link": {"type": "string", "format": "uri"}}},
                    "GoalShipped": {"fields": {}},
                },
            },
            "workflows": {
                "resolve": {
                    "entry": "arbitrate",
                    "steps": {
                        "arbitrate": {
                            "id": "resolve/arbitrate",
                            "agent": "Investigator",
                            "after": [],
                            "trigger": {"kind": "event", "event": "WorkItem"},
                            "output": {"goal": {"type": "string"}},
                        },
                        "fix": {
                            "id": "resolve/fix",
                            "agent": "Fixer",
                            "after": ["arbitrate"],
                            "emits": ["GoalShipped"],
                            "refs": [{"producer": "arbitrate", "field": "goal", "raw": "x"}],
                        },
                    },
                },
                "upkeep": {
                    "entry": "scan",
                    "steps": {
                        "scan": {
                            "id": "upkeep/scan",
                            "agent": "Investigator",
                            "trigger": {"kind": "cron", "expr": "0 3 * * *"},
                            "emits": ["WorkItem"],
                        }
                    },
                },
            },
            "sensors": [
                {
                    "name": "metric_watch",
                    "trigger": {"kind": "poll", "interval": "5m"},
                    "emits": "WorkItem",
                    "module": "sensors.s",
                    "fn": "metric_watch",
                },
                {
                    "name": "sentry",
                    "trigger": {"kind": "webhook", "path": "/hooks/sentry"},
                    "emits": "WorkItem",
                    "module": "sensors.s",
                    "fn": "sentry",
                },
            ],
            "lineage": {
                "events": {
                    "WorkItem": {"producers": ["upkeep", "metric_watch"], "consumers": ["resolve"]}
                }
            },
        }
    )


def test_build_meta_reports_presence_and_counts():
    assert build_meta(None) == {"manifest_present": False}
    meta = build_meta(_manifest())
    assert meta["manifest_present"] is True
    assert meta["schema_version"] == "1"
    assert meta["counts"]["workflows"] == 2
    assert meta["counts"]["agents"] == 1
    assert meta["counts"]["events"] == 2
    assert meta["counts"]["sensors"] == 2  # one poll + one webhook
    assert meta["counts"]["cron"] == 1  # the upkeep cron workflow


def test_build_registry_redacts_secrets():
    reg = build_registry(_manifest())
    sb = reg["sandboxes"][0]
    assert sb["name"] == "default"
    # the secret env_file paths/values must never appear — only a count.
    assert sb["secrets"] == {"redacted": True, "count": 2}
    assert "env_file" not in sb
    blob = repr(reg)
    assert "secrets/default.env" not in blob and "extra.env" not in blob
    # non-secret fields still surface
    assert sb["network"] == ["github.com"]
    assert sb["repos"][0]["url"] == "acme/runbooks"


def test_build_registry_shapes_agents_and_events():
    reg = build_registry(_manifest())
    agent = reg["agents"][0]
    assert agent == {
        "name": "Fixer",
        "runtime": "claude-code",
        "model": "claude-opus-4-8",
        "sandbox": "default",
        "skills": ["testing"],
    }
    work_item = next(e for e in reg["events"] if e["name"] == "WorkItem")
    assert work_item["fields"] == [
        {"name": "link", "type": "string", "format": "uri", "enum": None}
    ]


def test_build_workflows_dag_layers_edges_and_lineage():
    wfs = asyncio.run(build_workflows(_manifest(), InMemoryStateStore()))
    resolve = next(w for w in wfs["workflows"] if w["name"] == "resolve")
    assert resolve["entry"] == "arbitrate"
    assert resolve["trigger"] == {"kind": "event", "event": "WorkItem", "expr": None, "tz": None}
    # arbitrate has no deps (layer 0); fix depends on it (layer 1)
    assert resolve["generations"] == [["arbitrate"], ["fix"]]
    assert resolve["edges"] == [["arbitrate", "fix"]]
    assert resolve["schedule"] is None  # event-triggered, not a cron workflow
    assert wfs["lineage"]["WorkItem"]["consumers"] == ["resolve"]


def test_build_workflows_attaches_cron_schedule():
    async def go():
        store = InMemoryStateStore()
        await store.set_watermark("upkeep/scan", _at(0))  # a recorded last fire
        return await build_workflows(_manifest(), store)

    wfs = asyncio.run(go())
    upkeep = next(w for w in wfs["workflows"] if w["name"] == "upkeep")
    assert upkeep["trigger"]["kind"] == "cron"
    assert upkeep["schedule"]["last_run"] == _at(0).isoformat()
    assert upkeep["schedule"]["next_run"] is not None  # computed from the cron expr


def test_build_sensors_signature_and_emits():
    async def go():
        store = InMemoryStateStore()
        await store.set_watermark("metric_watch", _at(0))
        return await build_sensors(_manifest(), store)

    out = asyncio.run(go())
    poll = next(s for s in out["sensors"] if s["name"] == "metric_watch")
    assert poll["kind"] == "poll"
    assert poll["signature"] == "def metric_watch(req) -> WorkItem"
    assert poll["emits"] == "WorkItem" and poll["interval"] == "5m"
    assert poll["last_run"] == _at(0).isoformat() and poll["next_run"] is not None
    webhook = next(s for s in out["sensors"] if s["name"] == "sentry")
    assert webhook["kind"] == "webhook" and webhook["path"] == "/hooks/sentry"
    assert webhook["signature"] == "def sentry(req) -> WorkItem"


def test_build_sensors_without_manifest():
    out = asyncio.run(build_sensors(None, InMemoryStateStore()))
    assert out == {"manifest_present": False}


def test_manifest_endpoints_registered_and_served():
    app = create_app(InMemoryStateStore(), _manifest())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/api/meta", "/api/workflows", "/api/registry", "/api/sensors"} <= paths
    meta = asyncio.run(_endpoint(app, "/api/meta")())
    assert meta["manifest_present"] is True


def test_manifest_endpoints_empty_without_manifest():
    app = create_app(InMemoryStateStore())  # no manifest — back-compat with the old signature
    assert asyncio.run(_endpoint(app, "/api/meta")()) == {"manifest_present": False}
    assert asyncio.run(_endpoint(app, "/api/registry")()) == {"manifest_present": False}


def test_summary_to_dict_shape():
    store = _seed()
    rows = asyncio.run(store.list_runs())
    d = summary_to_dict(rows[0])
    assert set(d) == {
        "run_id",
        "workflow",
        "state",
        "entry_event",
        "created_at",
        "ended_at",
        "error",
    }
