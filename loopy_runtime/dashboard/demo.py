"""Throwaway demo data for the admin dashboard (`loopy demo`).

Synthesizes a manifest + a seeded in-memory store so the dashboard can be developed without a
real project, compile, or run. This whole module — and the `loopy demo` command that uses it —
is a development convenience and safe to delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loopy_runtime.contract import Event, RunEvent, StepOutput
from loopy_runtime.manifest_model import Manifest
from loopy_runtime.state.inmemory import InMemoryStateStore

# Anchored to real wall-clock now so "started N ago" and "next run in N" read live.
_NOW = datetime.now(UTC)


def _ago(**kw) -> datetime:
    return _NOW - timedelta(**kw)


def build_demo_manifest() -> Manifest:
    """A representative manifest: four workflows (incl. a parallel-step DAG), a cron workflow,
    agents, a sandbox with secret env files (to exercise redaction), events, limits, and
    poll + webhook sensors."""
    return Manifest.model_validate(
        {
            "schema_version": "1",
            "compiled_at": _ago(minutes=20).isoformat(),
            "loopy_version": "0.1.0-demo",
            "registry": {
                "agents": {
                    "Investigator": {
                        "harness": {"runtime": "claude-code", "model": "claude-sonnet-4-6"},
                        "sandbox": "default",
                        "skills": ["triage", "repro-authoring"],
                    },
                    "Fixer": {
                        "harness": {"runtime": "claude-code", "model": "claude-opus-4-8"},
                        "sandbox": "default",
                        "skills": ["testing"],
                    },
                    "Reviewer": {
                        "harness": {"runtime": "codex", "model": "gpt-5-codex"},
                        "sandbox": "default",
                        "skills": [],
                    },
                    "Releaser": {
                        "harness": {"runtime": "claude-code", "model": "claude-haiku-4-5"},
                        "sandbox": "default",
                        "skills": ["release"],
                    },
                },
                "sandboxes": {
                    "default": {
                        "provider": "daytona",
                        "image": {"debian_slim": "3.12", "apt": ["git"]},
                        "network": ["github.com", "api.sentry.io"],
                        "env_file": ["secrets/default.env", "secrets/sentry.env"],
                        "repos": [{"url": "acme/runbooks", "depth": 1}],
                    }
                },
                "events": {
                    "Incident": {
                        "fields": {
                            "source": {"enum": ["sentry", "pagerduty", "datadog"]},
                            "issue_id": {"type": "string", "format": "loopy-id"},
                            "title": {"type": "string"},
                            "link": {"type": "string", "format": "uri"},
                        }
                    },
                    "WorkItem": {
                        "fields": {
                            "link": {"type": "string", "format": "uri"},
                            "root_cause": {"type": "string"},
                            "proposed_goal": {"type": "string"},
                        }
                    },
                    "MetricThreshold": {"fields": {"goal_id": {"type": "string"}}},
                    "GoalShipped": {"fields": {"pr_url": {"type": "string", "format": "uri"}}},
                },
                "limits": {
                    "cascade_spend": {"usd": 50},
                    "workflows": {"resolve": {"spend": {"usd": 10}}},
                },
            },
            "workflows": {
                "triage": {
                    "entry": "investigate",
                    "steps": {
                        "investigate": {
                            "id": "triage/investigate",
                            "agent": "Investigator",
                            "trigger": {"kind": "event", "event": "Incident"},
                            "emits": ["WorkItem"],
                            "output": {"root_cause": {"type": "string"}},
                        }
                    },
                },
                "resolve": {
                    "entry": "arbitrate",
                    # arbitrate -> fix -> (review ‖ document) -> ship : shows a parallel DAG layer.
                    "steps": {
                        "arbitrate": {
                            "id": "resolve/arbitrate",
                            "agent": "Investigator",
                            "trigger": {"kind": "event", "event": "WorkItem"},
                            "output": {"goal": {"type": "string"}},
                        },
                        "fix": {
                            "id": "resolve/fix",
                            "agent": "Fixer",
                            "after": ["arbitrate"],
                            "output": {
                                "pr_url": {"type": "string", "format": "uri"},
                                "summary": {"type": "string"},
                            },
                        },
                        "review": {
                            "id": "resolve/review",
                            "agent": "Reviewer",
                            "after": ["fix"],
                            "output": {"verdict": {"enum": ["pass", "fail"]}},
                        },
                        "document": {
                            "id": "resolve/document",
                            "agent": "Investigator",
                            "after": ["fix"],
                            "output": {"notes": {"type": "string"}},
                        },
                        "ship": {
                            "id": "resolve/ship",
                            "agent": "Releaser",
                            "after": ["review", "document"],
                            "emits": ["GoalShipped"],
                        },
                    },
                },
                "upkeep": {
                    "entry": "scan-deps",
                    "steps": {
                        "scan-deps": {
                            "id": "upkeep/scan-deps",
                            "agent": "Investigator",
                            "trigger": {
                                "kind": "cron",
                                "expr": "0 3 * * *",
                                "tz": "America/New_York",
                            },
                            "emits": ["WorkItem"],
                        }
                    },
                },
                "confirm": {
                    "entry": "confirm-threshold",
                    "steps": {
                        "confirm-threshold": {
                            "id": "confirm/confirm-threshold",
                            "agent": "Investigator",
                            "trigger": {"kind": "event", "event": "MetricThreshold"},
                            "emits": ["WorkItem"],
                        }
                    },
                },
            },
            "sensors": [
                {
                    "name": "metric_watch",
                    "trigger": {"kind": "poll", "interval": "5m"},
                    "emits": "MetricThreshold",
                    "module": "sensors.sensors",
                    "fn": "metric_watch",
                },
                {
                    "name": "sentry_issues",
                    "trigger": {"kind": "webhook", "path": "/hooks/sentry"},
                    "emits": "Incident",
                    "module": "sensors.sensors",
                    "fn": "sentry_issues",
                },
            ],
            "lineage": {
                "events": {
                    "Incident": {
                        "producers": ["sentry_issues"],
                        "consumers": ["triage/investigate"],
                    },
                    "WorkItem": {
                        "producers": [
                            "triage/investigate",
                            "upkeep/scan-deps",
                            "confirm/confirm-threshold",
                        ],
                        "consumers": ["resolve/arbitrate"],
                    },
                    "MetricThreshold": {
                        "producers": ["metric_watch"],
                        "consumers": ["confirm/confirm-threshold"],
                    },
                    "GoalShipped": {"producers": ["resolve/ship"], "consumers": []},
                }
            },
        }
    )


async def seed_demo_store() -> InMemoryStateStore:
    """An in-memory store with a handful of runs across states — completed, running, failed —
    plus a watermark so the cron schedule shows a real last/next fire."""
    store = InMemoryStateStore()

    async def completed(run_id: str, event: str, t: datetime, steps: list[tuple[str, dict]]):
        await store.create_run(run_id, "1", Event(name=event, fields={}, id=run_id, emitted_at=t))
        await store.append(run_id, RunEvent("run_started", None, {"event": event}, t))
        for i, (step_id, out) in enumerate(steps, start=1):
            at = t + timedelta(seconds=20 * i)
            await store.append(run_id, RunEvent("step_completed", step_id, {}, at))
            await store.record_output(run_id, step_id, StepOutput(out))
        end = t + timedelta(seconds=20 * (len(steps) + 1))
        await store.append(
            run_id, RunEvent("event_emitted", steps[-1][0], {"event": "GoalShipped"}, end)
        )
        await store.append(run_id, RunEvent("run_completed", None, {}, end))

    await completed(
        "resolve-9f3a2b1c",
        "WorkItem",
        _ago(hours=2),
        [
            ("resolve/arbitrate", {"goal": "Fix flaky retry in webhook ingest"}),
            (
                "resolve/fix",
                {
                    "pr_url": "https://github.com/acme/app/pull/412",
                    "summary": "Add jittered backoff",
                },
            ),
            ("resolve/review", {"verdict": "pass"}),
            ("resolve/document", {"notes": "Updated the on-call runbook."}),
            ("resolve/ship", {}),
        ],
    )
    await completed(
        "triage-7c1d4e88",
        "Incident",
        _ago(hours=1, minutes=10),
        [("triage/investigate", {"root_cause": "Upstream rate-limit, 429 storm"})],
    )

    # A run still in flight (started, one step done, no terminal event).
    rid = "resolve-3b8e0a52"
    t = _ago(minutes=4)
    await store.create_run(rid, "1", Event(name="WorkItem", fields={}, id=rid, emitted_at=t))
    await store.append(rid, RunEvent("run_started", None, {"event": "WorkItem"}, t))
    await store.append(
        rid, RunEvent("step_completed", "resolve/arbitrate", {}, t + timedelta(seconds=30))
    )
    await store.record_output(
        rid, "resolve/arbitrate", StepOutput({"goal": "Patch the N+1 in the digest job"})
    )

    # A failed run.
    rid = "upkeep-d40f1a09"
    t = _ago(hours=11)
    await store.create_run(
        rid, "1", Event(name="cron:upkeep/scan-deps", fields={}, id=rid, emitted_at=t)
    )
    await store.append(rid, RunEvent("run_started", None, {"event": "cron:upkeep/scan-deps"}, t))
    await store.append(
        rid,
        RunEvent(
            "run_failed",
            "upkeep/scan-deps",
            {"error": "sandbox acquire timed out after 120s (daytona)"},
            t + timedelta(seconds=125),
        ),
    )

    # Cron last-fire watermark — yesterday 03:00 ET.
    await store.set_watermark("upkeep/scan-deps", _ago(hours=11, minutes=30))

    # The store stamps each run's list-view `created_at` at wall-clock now; align it with the
    # run's first event so the table's Started/Duration columns match the seeded timeline.
    for run_id, hist in store._history.items():
        if hist:
            wf, entry, _ = store._meta[run_id]
            store._meta[run_id] = (wf, entry, hist[0].at)
    return store
