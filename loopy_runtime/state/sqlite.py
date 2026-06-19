"""SQLite-backed StateStore (B8, step toward B10) — a single on-disk file that survives
restarts and that a *separate* process can read.

This is the substrate for the control-plane dashboard: `loopy run` opens it read-write and
records run history/outputs as the cascade executes; `loopy admin` opens the same file
read-only and serves it. WAL mode lets the reader and writer coexist on one host (single-host
by design — a networked store, Redis/Postgres, slots behind the same Protocol later for
multi-host B10/B11).

State stays single-sourced in the event history: the denormalized `runs` row is just an index
for the list view, updated from the terminal `run_completed`/`run_failed` append so it always
agrees with `state.derive.terminal_state` over the same history.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from loopy_runtime.contract import (
    Event,
    RunEvent,
    RunId,
    RunSummary,
    StepId,
    StepOutput,
    TriggerId,
)
from loopy_runtime.state.inmemory import _workflow_of

# History `kind`s that end a run, mapped to the resulting RunSummary state (mirrors
# state.derive._TERMINAL — kept here because the SQLite store flips the index on append
# rather than deriving on read).
_TERMINAL: dict[str, str] = {"run_completed": "completed", "run_failed": "failed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    workflow         TEXT NOT NULL,
    manifest_version TEXT,
    entry_event      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    state            TEXT NOT NULL,
    ended_at         TEXT,
    error            TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    kind    TEXT NOT NULL,
    step_id TEXT,
    payload TEXT NOT NULL,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_by_run ON run_events (run_id, id);
CREATE TABLE IF NOT EXISTS step_outputs (
    run_id  TEXT NOT NULL,
    step_id TEXT NOT NULL,
    fields  TEXT NOT NULL,
    PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS watermarks (trigger_id TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY);
"""


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class SqliteStateStore:
    """A StateStore persisted to a single SQLite file. Open read-write for `loopy run`;
    pass `read_only=True` (the dashboard) to open an existing file without writing to it."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            uri = f"file:{self.path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
        self._conn.row_factory = sqlite3.Row
        # WAL: a single writer and concurrent readers across processes on one host.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    # ── run history ──────────────────────────────────────────────────────────────
    async def create_run(self, run_id: RunId, manifest_version: str, entry: Event) -> None:
        # INSERT OR IGNORE so a replayed create_run (at-least-once delivery) doesn't reset a
        # run that already advanced.
        self._conn.execute(
            "INSERT OR IGNORE INTO runs "
            "(run_id, workflow, manifest_version, entry_event, created_at, state) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (run_id, _workflow_of(run_id), manifest_version, entry.name, _iso(datetime.now(UTC))),
        )
        self._conn.commit()

    async def append(self, run_id: RunId, ev: RunEvent) -> None:
        self._conn.execute(
            "INSERT INTO run_events (run_id, kind, step_id, payload, at) VALUES (?, ?, ?, ?, ?)",
            (run_id, ev.kind, ev.step_id, json.dumps(ev.payload, default=str), _iso(ev.at)),
        )
        terminal = _TERMINAL.get(ev.kind)
        if terminal is not None:  # flip the index row to match the derived terminal state
            error = ev.payload.get("error") if terminal == "failed" else None
            self._conn.execute(
                "UPDATE runs SET state = ?, ended_at = ?, error = ? WHERE run_id = ?",
                (terminal, _iso(ev.at), error, run_id),
            )
        self._conn.commit()

    async def history(self, run_id: RunId) -> list[RunEvent]:
        rows = self._conn.execute(
            "SELECT kind, step_id, payload, at FROM run_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            RunEvent(
                kind=r["kind"],
                step_id=r["step_id"],
                payload=json.loads(r["payload"]),
                at=_parse(r["at"]),
            )
            for r in rows
        ]

    async def list_runs(
        self, *, limit: int = 100, offset: int = 0, state: str | None = None
    ) -> list[RunSummary]:
        sql = (
            "SELECT run_id, workflow, state, entry_event, created_at, ended_at, error "
            "FROM runs"
        )
        params: list[object] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = self._conn.execute(sql, params).fetchall()
        return [
            RunSummary(
                run_id=r["run_id"],
                workflow=r["workflow"],
                state=r["state"],
                entry_event=r["entry_event"],
                created_at=_parse(r["created_at"]),
                ended_at=_parse(r["ended_at"]) if r["ended_at"] else None,
                error=r["error"],
            )
            for r in rows
        ]

    # ── step outputs ─────────────────────────────────────────────────────────────
    async def record_output(self, run_id: RunId, step_id: StepId, out: StepOutput) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO step_outputs (run_id, step_id, fields) VALUES (?, ?, ?)",
            (run_id, step_id, json.dumps(out.fields, default=str)),
        )
        self._conn.commit()

    async def outputs(self, run_id: RunId) -> Mapping[StepId, StepOutput]:
        rows = self._conn.execute(
            "SELECT step_id, fields FROM step_outputs WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {r["step_id"]: StepOutput(json.loads(r["fields"])) for r in rows}

    # ── watermarks (poll/cron) ───────────────────────────────────────────────────
    async def get_watermark(self, t: TriggerId) -> datetime | None:
        row = self._conn.execute(
            "SELECT ts FROM watermarks WHERE trigger_id = ?", (t,)
        ).fetchone()
        return _parse(row["ts"]) if row else None

    async def set_watermark(self, t: TriggerId, ts: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO watermarks (trigger_id, ts) VALUES (?, ?)", (t, _iso(ts))
        )
        self._conn.commit()

    # ── dedupe (at-least-once delivery) ──────────────────────────────────────────
    async def seen(self, key: str) -> bool:
        return self._conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone() is not None

    async def mark_seen(self, key: str) -> None:
        self._conn.execute("INSERT OR IGNORE INTO seen (key) VALUES (?)", (key,))
        self._conn.commit()
