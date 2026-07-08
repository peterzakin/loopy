"""P9 — serialize a `Project` to the deterministic manifest.

Plain JSON-able dicts/lists; the CLI serializes with sorted keys so the document
is byte-stable and content-hashable. `compiled_at`/`loopy_version` are stamped by
the CLI wrapper, outside this hashed core.
"""

from __future__ import annotations

from loopy_core.compile.model import Project
from loopy_core.registry.model import Agent, Event, Registry, Sandbox
from loopy_core.workflow.model import Budget, Ref, Step, Trigger, Workflow

SCHEMA_VERSION = "2"


def to_manifest(project: Project) -> dict:
    """Emit the §9 manifest shape (without CLI-stamped fields)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "registry": _registry(project.registry),
        "workflows": {name: _workflow(wf) for name, wf in project.workflows.items()},
        "sensors": [_sensor(s) for s in sorted(project.sensors, key=lambda s: s.name)],
        "lineage": {
            "events": {
                name: {"producers": lin.producers, "consumers": lin.consumers}
                for name, lin in project.lineage.events.items()
            }
        },
    }


def _registry(registry: Registry) -> dict:
    out = {
        "sandboxes": {name: _sandbox(sb) for name, sb in registry.sandboxes.items()},
        "agents": {name: _agent(ag) for name, ag in registry.agents.items()},
        "events": {name: _event(ev) for name, ev in registry.events.items()},
    }
    if registry.limits is not None:
        out["limits"] = {
            "cascade_spend": registry.limits.cascade_spend,
            "workflows": {
                name: {"spend": wl.spend} for name, wl in registry.limits.workflows.items()
            },
        }
    return out


def _sandbox(sb: Sandbox) -> dict:
    return {
        "provider": sb.provider,
        "image": sb.image,
        "network": sb.network,
        "env_file": sb.env_file,
        "repos": [{"url": r.url, "ref": r.ref, "path": r.path, "depth": r.depth} for r in sb.repos],
    }


def _agent(ag: Agent) -> dict:
    return {
        "model": ag.model,
        "harness": ag.harness,
        "sandbox": ag.sandbox,
        "skills": ag.skills,
    }


def _event(ev: Event) -> dict:
    return {"fields": ev.fields}


def _workflow(wf: Workflow) -> dict:
    return {
        "entry": wf.entry,
        "steps": {name: _step(step) for name, step in wf.steps.items()},
    }


def _step(step: Step) -> dict:
    return {
        "id": step.id,
        "trigger": _trigger(step.trigger),
        "after": step.after,
        "agent": step.agent,
        "output": step.output,
        "emits": step.emits,
        "budget": _budget(step.budget),
        "body": step.body,
        "refs": [_ref(r) for r in step.refs],
    }


def _trigger(trigger: Trigger | None) -> dict | None:
    if trigger is None:
        return None
    if trigger.kind == "cron":
        return {"kind": "cron", "expr": trigger.expr, "tz": trigger.tz}
    return {"kind": "event", "event": trigger.event, "filters": dict(trigger.filters)}


def _budget(budget: Budget | None) -> dict | None:
    if budget is None:
        return None
    return {
        k: v
        for k, v in {
            "wall_clock": budget.wall_clock,
            "spend": budget.spend,
            "window": budget.window,
            "latency": budget.latency,
        }.items()
        if v is not None
    }


def _ref(ref: Ref) -> dict:
    return {"producer": ref.producer, "field": ref.field, "raw": ref.raw}


def _sensor(sensor) -> dict:
    trigger = (
        {"kind": "webhook", "path": sensor.trigger.path}
        if sensor.trigger.kind == "webhook"
        else {"kind": "poll", "interval": sensor.trigger.interval}
    )
    return {
        "name": sensor.name,
        "trigger": trigger,
        "emits": sensor.emits,
        "source": sensor.source,
        "provider": sensor.provider,
        "module": sensor.module,
        "fn": sensor.fn,
    }
