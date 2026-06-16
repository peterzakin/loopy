"""P6 — statically resolve each extracted `Ref` against its producing node.

`event.<field>` resolves to the run's triggering event (the workflow's entry
trigger); `<step>.<field>` resolves to a *direct* `after:` predecessor's output.
Validates existence only (T4 type-compat is deferred). The restricted grammar +
direct-only refs make resolution total.
"""

from __future__ import annotations

from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.registry.model import Registry
from loopy_core.workflow.model import Workflow

# Cron triggers expose only these built-in fields (T2).
_CRON_FIELDS = frozenset({"scheduled_at", "last_run"})


def resolve_refs(
    workflows: dict[str, Workflow], registry: Registry, diags: DiagnosticCollector
) -> None:
    for workflow in workflows.values():
        entry = workflow.steps.get(workflow.entry) if workflow.entry else None
        entry_trigger = entry.trigger if entry else None

        for step in workflow.steps.values():
            for ref in step.refs:
                if ref.producer == "event":
                    _resolve_event_ref(ref, entry_trigger, registry, diags)
                else:
                    _resolve_step_ref(ref, step, workflow, diags)


def _resolve_event_ref(ref, entry_trigger, registry: Registry, diags) -> None:
    if entry_trigger is None:
        return  # no entry (W2 already fired); avoid cascading noise

    if entry_trigger.kind == "cron":
        # T2 — cron triggers expose only scheduled_at / last_run.
        if ref.field not in _CRON_FIELDS:
            diags.error(
                codes.E303,
                f"cron trigger exposes only {sorted(_CRON_FIELDS)}, not 'event.{ref.field}'",
                span=ref.span,
            )
        return

    # T1 — event.<field> must exist in the triggering event's contract.
    event = registry.events.get(entry_trigger.event)
    if event is None:
        return  # unregistered event is reported as E504 in M5; don't double-report
    if ref.field not in event.fields:
        diags.error(
            codes.E302,
            f"'{ref.field}' is not a field of event '{entry_trigger.event}'",
            span=ref.span,
        )


def _resolve_step_ref(ref, step, workflow: Workflow, diags) -> None:
    # T3 — <step> must be a direct after: predecessor.
    if ref.producer not in step.after:
        diags.error(
            codes.E304,
            f"'{ref.producer}' is not a direct after: predecessor of step '{step.id}'",
            span=ref.span,
            hint="reference only a direct after: predecessor; re-add it to after: to reach it",
        )
        return

    producer = workflow.steps.get(ref.producer)
    if producer is None:
        return  # missing after: target already reported as E105
    # T3 — <field> must be a top-level key of that step's output: schema.
    if ref.field not in producer.output:
        diags.error(
            codes.E305,
            f"'{ref.field}' is not a top-level key of step '{producer.id}' output:",
            span=ref.span,
        )
