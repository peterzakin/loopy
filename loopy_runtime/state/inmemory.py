"""In-memory StateStore — dict-backed, lost on restart (v1; no durability).

Behind the real `StateStore` Protocol so a SQLite/Postgres/Temporal-history store
drops in later for B10/B11.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime

from loopy_runtime.contract import (
    Event,
    RunEvent,
    RunId,
    RunSummary,
    StepId,
    StepOutput,
    TriggerId,
)
from loopy_runtime.state.derive import terminal_state, workflow_of


class InMemoryStateStore:
    def __init__(self) -> None:
        self._history: dict[RunId, list[RunEvent]] = defaultdict(list)
        self._outputs: dict[RunId, dict[StepId, StepOutput]] = defaultdict(dict)
        # Per-run metadata captured at create_run, in creation order (dict preserves insertion
        # order) — so list_runs can page newest-first without scanning history for it.
        self._meta: dict[RunId, tuple[str, str, datetime]] = {}  # run_id -> (wf, entry, created)
        self._watermarks: dict[TriggerId, datetime] = {}
        self._seen: set[str] = set()

    async def create_run(self, run_id: RunId, manifest_version: str, entry: Event) -> None:
        self._history.setdefault(run_id, [])
        self._meta.setdefault(run_id, (workflow_of(run_id), entry.name, datetime.now(UTC)))

    async def append(self, run_id: RunId, ev: RunEvent) -> None:
        self._history[run_id].append(ev)

    async def history(self, run_id: RunId) -> list[RunEvent]:
        return list(self._history[run_id])

    async def list_runs(
        self, *, limit: int = 100, offset: int = 0, state: str | None = None
    ) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        for run_id, (workflow, entry_event, created_at) in self._meta.items():
            run_state, ended_at, error = terminal_state(self._history[run_id])
            summaries.append(
                RunSummary(
                    run_id=run_id,
                    workflow=workflow,
                    state=run_state,
                    entry_event=entry_event,
                    created_at=created_at,
                    ended_at=ended_at,
                    error=error,
                )
            )
        summaries.reverse()  # creation order -> newest first
        if state is not None:
            summaries = [s for s in summaries if s.state == state]
        return summaries[offset : offset + limit]

    async def record_output(self, run_id: RunId, step_id: StepId, out: StepOutput) -> None:
        self._outputs[run_id][step_id] = out

    async def outputs(self, run_id: RunId) -> Mapping[StepId, StepOutput]:
        return dict(self._outputs[run_id])

    async def get_watermark(self, t: TriggerId) -> datetime | None:
        return self._watermarks.get(t)

    async def set_watermark(self, t: TriggerId, ts: datetime) -> None:
        self._watermarks[t] = ts

    async def seen(self, key: str) -> bool:
        return key in self._seen

    async def mark_seen(self, key: str) -> None:
        self._seen.add(key)
