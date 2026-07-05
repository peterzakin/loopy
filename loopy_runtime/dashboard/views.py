"""Pure view builders for the dashboard read API (B12): turn StateStore value types into
JSON-able dicts. No I/O and no FastAPI here, so the shaping is unit-testable on its own and the
route handlers in `app.py` stay thin.

The run *detail* is built entirely from `history` + `outputs` — state/workflow/timestamps are
derived (via `state.derive`) rather than read from a second place — so the view is store-agnostic
and can't disagree with the list view about what a run's state is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loopy_runtime.contract import RunEvent, RunSummary, StepId, StepOutput
from loopy_runtime.state.derive import terminal_state, workflow_of

if TYPE_CHECKING:
    from loopy_runtime.contract import StateStore
    from loopy_runtime.manifest_model import Manifest, SandboxSpec, StepSpec, WorkflowSpec

# The run states the list filter accepts — same vocabulary as RunSummary.state.
VALID_RUN_STATES = ("running", "completed", "failed")


def _iso(value: Any) -> Any:
    """ISO-format a datetime; pass through None (and anything already a string)."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def summary_to_dict(s: RunSummary) -> dict[str, Any]:
    """One row of the run list."""
    return {
        "run_id": s.run_id,
        "workflow": s.workflow,
        "state": s.state,
        "entry_event": s.entry_event,
        "created_at": _iso(s.created_at),
        "ended_at": _iso(s.ended_at),
        "error": s.error,
    }


def event_to_dict(e: RunEvent) -> dict[str, Any]:
    """One entry in a run's timeline."""
    return {"kind": e.kind, "step_id": e.step_id, "payload": dict(e.payload), "at": _iso(e.at)}


def _entry_event(history: list[RunEvent]) -> str | None:
    """The triggering event name, recorded in the `run_started` payload."""
    for e in history:
        if e.kind == "run_started":
            return e.payload.get("event")
    return None


def _step_states(history: list[RunEvent]) -> dict[StepId, str]:
    """Per-step outcome derived from the timeline: a `step_completed` marks a step done; the
    `step_id` on a `run_failed` marks the step the run died on."""
    states: dict[StepId, str] = {}
    for e in history:
        if e.kind == "step_completed" and e.step_id:
            states[e.step_id] = "completed"
        elif e.kind == "run_failed" and e.step_id:
            states[e.step_id] = "failed"
    return states


def build_run_detail(
    run_id: str, history: list[RunEvent], outputs: Mapping[StepId, StepOutput]
) -> dict[str, Any]:
    """The full detail for one run: derived status, step outcomes, emitted events, the raw
    timeline, and each step's validated output — everything 'what happened in this run' needs."""
    state, ended_at, error = terminal_state(history)
    return {
        "run_id": run_id,
        "workflow": workflow_of(run_id),
        "state": state,
        "entry_event": _entry_event(history),
        "created_at": _iso(history[0].at) if history else None,
        "ended_at": _iso(ended_at),
        "error": error,
        "steps": _step_states(history),
        "emitted": [e.payload.get("event") for e in history if e.kind == "event_emitted"],
        "history": [event_to_dict(e) for e in history],
        "outputs": {step_id: dict(out.fields) for step_id, out in outputs.items()},
    }


# ── manifest views (templates / registry / schedules) ────────────────────────────────
#
# These read the *compiled manifest* — the static definition of the system — rather than
# run state. They power the workflow-template, registry, and schedule views. The manifest
# holds no secret *values* (only references, e.g. `env_file` paths), and even those we
# redact here so the dashboard can never surface them.


def build_meta(manifest: Manifest | None) -> dict[str, Any]:
    """Top-level summary: is a manifest loaded, and how big is the system?"""
    if manifest is None:
        return {"manifest_present": False}
    reg = manifest.registry
    return {
        "manifest_present": True,
        "schema_version": manifest.schema_version,
        "compiled_at": manifest.compiled_at,
        "loopy_version": manifest.loopy_version,
        "counts": {
            "workflows": len(manifest.workflows),
            "agents": len(reg.agents),
            "sandboxes": len(reg.sandboxes),
            "events": len(reg.events),
            "sensors": len(manifest.sensors),
            "cron": len(manifest.cron_entries()),
        },
    }


def _field_rows(fields: dict[str, dict]) -> list[dict[str, Any]]:
    """Flatten a JSON-Schema-ish field map into ordered display rows."""
    rows: list[dict[str, Any]] = []
    for name, spec in fields.items():
        rows.append(
            {
                "name": name,
                "type": spec.get("type"),
                "format": spec.get("format"),
                "enum": spec.get("enum"),
            }
        )
    return rows


def _redact_sandbox(name: str, sb: SandboxSpec) -> dict[str, Any]:
    """A sandbox view with secrets removed. `env_file` is a list of paths to gitignored secret
    files; we never expose the paths or values — only that N are configured. `env` is the
    passthrough allow-list: variable *names* only (no values, compiler-guaranteed non-reserved),
    so the names are safe to surface and show which credentials the sandbox receives."""
    return {
        "name": name,
        "provider": sb.provider,
        "image": dict(sb.image),
        "network": list(sb.network),
        "repos": [{"url": r.url, "ref": r.ref, "path": r.path, "depth": r.depth} for r in sb.repos],
        "env": list(sb.env),
        "secrets": {"redacted": True, "count": len(sb.env_file)},
    }


def build_registry(manifest: Manifest | None) -> dict[str, Any]:
    """The registry entities — agents, sandboxes, events, limits — with secrets redacted."""
    if manifest is None:
        return {"manifest_present": False}
    reg = manifest.registry
    agents = [
        {
            "name": name,
            "runtime": a.harness,
            "model": a.model,
            "sandbox": a.sandbox,
            "skills": list(a.skills),
        }
        for name, a in sorted(reg.agents.items())
    ]
    sandboxes = [_redact_sandbox(name, sb) for name, sb in sorted(reg.sandboxes.items())]
    events = [
        {"name": name, "fields": _field_rows(ev.fields)} for name, ev in sorted(reg.events.items())
    ]
    limits = None
    if reg.limits is not None:
        limits = {
            "cascade_spend": reg.limits.cascade_spend,
            "workflows": {k: v.spend for k, v in reg.limits.workflows.items()},
        }
    return {
        "manifest_present": True,
        "agents": agents,
        "sandboxes": sandboxes,
        "events": events,
        "limits": limits,
    }


def _generations(steps: dict[str, StepSpec]) -> list[list[str]]:
    """Group local step names into topological layers by their `after` deps — each layer can run
    in parallel, and the layers give the DAG its left-to-right (or top-down) columns. Post-compile
    the graph is acyclic; the guard only keeps a corrupt manifest from looping forever."""
    remaining = {name: set(s.after) for name, s in steps.items()}
    placed: set[str] = set()
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(n for n, deps in remaining.items() if deps <= placed)
        if not layer:  # cycle / dangling dep — surface the rest rather than spin
            layer = sorted(remaining)
        layers.append(layer)
        placed |= set(layer)
        for n in layer:
            remaining.pop(n)
    return layers


def _step_view(name: str, step: StepSpec) -> dict[str, Any]:
    return {
        "name": name,
        "id": step.id,
        "agent": step.agent,
        "after": list(step.after),
        "emits": list(step.emits),
        "trigger": step.trigger.model_dump() if step.trigger else None,
        "outputs": _field_rows(step.output),
        "refs": [{"producer": r.producer, "field": r.field} for r in step.refs],
        "body": step.body,
    }


def _workflow_view(name: str, wf: WorkflowSpec) -> dict[str, Any]:
    entry = wf.steps.get(wf.entry) if wf.entry else None
    edges = [[dep, sname] for sname, s in wf.steps.items() for dep in s.after]
    return {
        "name": name,
        "entry": wf.entry,
        "trigger": entry.trigger.model_dump() if entry and entry.trigger else None,
        "steps": [_step_view(sname, s) for sname, s in wf.steps.items()],
        "edges": edges,
        "generations": _generations(wf.steps),
    }


async def build_workflows(manifest: Manifest | None, store: StateStore) -> dict[str, Any]:
    """Every workflow as a DAG (steps + edges + topological layers), plus the cross-workflow event
    lineage. Cron-triggered workflows also carry their schedule — last fire (stored watermark) and
    computed next fire — so the workflows view can group them into their own section."""
    if manifest is None:
        return {"manifest_present": False}

    # Imported lazily — keeps the dashboard importable without the scheduler's deps at module load.
    from loopy_runtime.sensors.scheduler import cron_next

    now = datetime.now(UTC)
    workflows: list[dict[str, Any]] = []
    for name, wf in sorted(manifest.workflows.items()):
        view = _workflow_view(name, wf)
        trig = view["trigger"]
        if trig and trig.get("kind") == "cron":
            entry = wf.steps.get(wf.entry) if wf.entry else None
            last = await store.get_watermark(entry.id) if entry else None
            next_run = None
            if trig.get("expr"):
                try:
                    next_run = cron_next(trig["expr"], last or now, trig.get("tz"))
                except Exception:  # noqa: BLE001 — a bad expr shouldn't 500 the view
                    next_run = None
            view["schedule"] = {"last_run": _iso(last), "next_run": _iso(next_run)}
        else:
            view["schedule"] = None
        workflows.append(view)

    lineage = {
        name: {"producers": list(e.producers), "consumers": list(e.consumers)}
        for name, e in manifest.lineage.events.items()
    }
    return {"manifest_present": True, "workflows": workflows, "lineage": lineage}


def _signature(s) -> str:
    """A representative source signature for a sensor. By convention a sensor takes a request-like
    argument and returns its emitted event (see examples/*/sensors). Built-ins have no user
    function — show the platform provider they're served by instead."""
    if getattr(s, "source", "module") == "builtin":
        return f"# built-in {s.provider} sensor -> {s.emits}"
    return f"def {s.fn}(req) -> {s.emits}"


async def build_sensors(manifest: Manifest | None, store: StateStore) -> dict[str, Any]:
    """Sensors — the project's inputs — with their function signature and emitted event. Poll
    sensors also carry last/next fire; webhook sensors carry their inbound path."""
    if manifest is None:
        return {"manifest_present": False}

    from loopy_runtime.sensors.scheduler import parse_interval

    now = datetime.now(UTC)
    sensors: list[dict[str, Any]] = []
    for s in manifest.sensors:
        item: dict[str, Any] = {
            "name": s.name,
            "kind": s.trigger.kind,
            "module": s.module,
            "fn": s.fn,
            "qualname": f"{s.module}.{s.fn}" if s.module and s.fn else s.name,
            "emits": s.emits,
            "signature": _signature(s),
        }
        if s.trigger.kind == "poll":
            last = await store.get_watermark(s.name)
            next_run = None
            if s.trigger.interval:
                try:
                    next_run = (last or now) + parse_interval(s.trigger.interval)
                except Exception:  # noqa: BLE001
                    next_run = None
            item["interval"] = s.trigger.interval
            item["last_run"] = _iso(last)
            item["next_run"] = _iso(next_run)
        elif s.trigger.kind == "webhook":
            item["path"] = s.trigger.path
        sensors.append(item)
    return {"manifest_present": True, "sensors": sensors}
