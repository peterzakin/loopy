"""In-memory StateStore — dict-backed, lost on restart (v1; no durability).

Behind the real `StateStore` Protocol so a SQLite/Postgres/Temporal-history store
drops in later for B10/B11.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime

from loopy_runtime.contract import Event, RunEvent, RunId, StepId, StepOutput, TriggerId


class InMemoryStateStore:
    def __init__(self) -> None:
        self._history: dict[RunId, list[RunEvent]] = defaultdict(list)
        self._outputs: dict[RunId, dict[StepId, StepOutput]] = defaultdict(dict)
        self._watermarks: dict[TriggerId, datetime] = {}
        self._seen: set[str] = set()

    async def create_run(self, run_id: RunId, manifest_version: str, entry: Event) -> None:
        self._history.setdefault(run_id, [])

    async def append(self, run_id: RunId, ev: RunEvent) -> None:
        self._history[run_id].append(ev)

    async def history(self, run_id: RunId) -> list[RunEvent]:
        return list(self._history[run_id])

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
