"""Shared run-state derivation: turn an event-sourced history into a run's terminal state, and
parse a run's workflow from its id.

Used by every StateStore implementation so 'is this run running/completed/failed, when did it
end, and why' — and 'which workflow is this' — are computed one way everywhere. The SQLite store
denormalizes the same result into its `runs` index on append (via `terminal_for_event`); this
module is the single source of truth those stores agree through.
"""

from __future__ import annotations

from datetime import datetime

from loopy_runtime.contract import RunEvent, RunId

# History `kind`s that end a run, mapped to the resulting RunSummary state. The one place this
# mapping lives — add a new terminal kind here and every store picks it up.
_TERMINAL: dict[str, str] = {"run_completed": "completed", "run_failed": "failed"}


def workflow_of(run_id: RunId) -> str:
    """The workflow name from a `f"{wf_name}-{seq}"` run id (seq is an int, so split off the
    trailing `-<seq>`); falls back to the whole id if it doesn't match that shape."""
    head, _, tail = run_id.rpartition("-")
    return head if head and tail.isdigit() else run_id


def terminal_for_event(ev: RunEvent) -> tuple[str, str | None] | None:
    """If `ev` ends a run, return `(state, error)`; otherwise None. `error` is set only for a
    failure. Both the per-event SQLite index update and the per-history scan below go through
    this, so they can't disagree on what 'terminal' means."""
    state = _TERMINAL.get(ev.kind)
    if state is None:
        return None
    error = ev.payload.get("error") if state == "failed" else None
    return state, error


def terminal_state(history: list[RunEvent]) -> tuple[str, datetime | None, str | None]:
    """Return (state, ended_at, error) for a run from its history. A run is `running` until a
    terminal entry appears; scans for the first terminal entry (a run ends once)."""
    for ev in history:
        terminal = terminal_for_event(ev)
        if terminal is not None:
            state, error = terminal
            return state, ev.at, error
    return "running", None, None
