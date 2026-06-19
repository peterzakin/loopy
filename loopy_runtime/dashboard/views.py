"""Pure view builders for the dashboard read API (B12): turn StateStore value types into
JSON-able dicts. No I/O and no FastAPI here, so the shaping is unit-testable on its own and the
route handlers in `app.py` stay thin.

The run *detail* is built entirely from `history` + `outputs` — state/workflow/timestamps are
derived (via `state.derive`) rather than read from a second place — so the view is store-agnostic
and can't disagree with the list view about what a run's state is.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loopy_runtime.contract import RunEvent, RunSummary, StepId, StepOutput
from loopy_runtime.state.derive import terminal_state, workflow_of

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
