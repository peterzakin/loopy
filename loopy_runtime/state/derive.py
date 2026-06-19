"""Shared run-state derivation: turn an event-sourced history into a run's terminal state.

Used by the StateStore implementations so 'is this run running/completed/failed, when did it
end, and why' is computed one way everywhere — a view over the history, never a second source
of truth. The SQLite store denormalizes the same result into its `runs` index on append; this
keeps that index in lockstep with what the in-memory store derives on read.
"""

from __future__ import annotations

from datetime import datetime

from loopy_runtime.contract import RunEvent

# History `kind`s that end a run, mapped to the resulting RunSummary state.
_TERMINAL: dict[str, str] = {"run_completed": "completed", "run_failed": "failed"}


def terminal_state(history: list[RunEvent]) -> tuple[str, datetime | None, str | None]:
    """Return (state, ended_at, error) for a run from its history.

    A run is `running` until a terminal entry appears; `run_failed` carries its `error` in the
    payload. Scans for the first terminal entry (a run ends once)."""
    for ev in history:
        state = _TERMINAL.get(ev.kind)
        if state is not None:
            error = ev.payload.get("error") if state == "failed" else None
            return state, ev.at, error
    return "running", None, None
