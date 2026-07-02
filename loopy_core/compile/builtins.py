"""Inject built-in events + their producing sensors (Option A).

A workflow may trigger on a platform-shipped event (`on: Github.PullRequestOpened`)
without declaring it or authoring a sensor. This pass — run after workflows/sensors
are loaded and *before* `resolve_refs` and `cross_check` — does:

  1. Guards the reserved namespace: a user-declared event or user sensor in `Github.*`
     is an error (E215). The platform owns producers in that namespace.
  2. For each `Github.*` event a workflow triggers on, registers its contract (so
     `{{ event.field }}` refs resolve and the runtime can validate it) and synthesizes
     a built-in `Sensor` on `/hooks/github` (so the event has a producer — no W501).
  3. Reports an unknown `Github.*` trigger (E112) with the catalog of known names.

Agents are never injected — every agent is declared explicitly in `registry.yml`
(`loopy init` scaffolds one per supported runtime); an unregistered `agent:` is E501.

Validation of the reserved namespace lives entirely here; `cross_check` skips its
E504 registration check for reserved-prefix events (this pass owns them).
"""

from __future__ import annotations

from loopy_core.builtins import (
    GITHUB_EVENTS,
    GITHUB_PREFIX,
    is_reserved,
)
from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.registry.model import Event, Registry
from loopy_core.registry.types import desugar
from loopy_core.sensors.model import Sensor, SensorTrigger
from loopy_core.span import Span, span_at
from loopy_core.workflow.model import Workflow

_BUILTIN_PATH = "/hooks/github"
_BUILTIN_SPAN = span_at("<built-in>")


def inject_builtins(
    registry: Registry,
    workflows: dict[str, Workflow],
    sensors: list[Sensor],
    diags: DiagnosticCollector,
) -> list[Sensor]:
    """Register referenced built-in events into `registry` and return the synthesized
    built-in sensors to append to the project's sensor list."""
    _guard_reserved(registry, workflows, sensors, diags)

    # Collect referenced built-in trigger events, keeping a span for diagnostics.
    referenced: dict[str, Span] = {}
    for workflow in workflows.values():
        for step in workflow.steps.values():
            trig = step.trigger
            if trig is not None and trig.kind == "event" and trig.event and is_reserved(trig.event):
                referenced.setdefault(trig.event, trig.span or step.span)

    built: list[Sensor] = []
    for name in sorted(referenced):
        contract = GITHUB_EVENTS.get(name)
        if contract is None:
            known = ", ".join(sorted(GITHUB_EVENTS))
            diags.error(
                codes.E112,
                f"unknown built-in event '{name}'; known built-ins: {known}",
                span=referenced[name],
            )
            continue
        if name not in registry.events:
            registry.events[name] = _event(name, contract, diags)
        built.append(
            Sensor(
                name=f"builtin:{name}",
                trigger=SensorTrigger(kind="webhook", path=_BUILTIN_PATH, span=_BUILTIN_SPAN),
                emits=name,
                source="builtin",
                provider="github",
                span=_BUILTIN_SPAN,
            )
        )
    return built


def _event(name: str, contract: dict[str, str], diags: DiagnosticCollector) -> Event:
    fields: dict[str, dict] = {}
    for fname, ftype in contract.items():
        schema = desugar(ftype, span=_BUILTIN_SPAN, diags=diags)
        if schema is not None:
            fields[fname] = schema
    return Event(name=name, fields=fields, builtin=True, span=_BUILTIN_SPAN)


def _guard_reserved(
    registry: Registry,
    workflows: dict[str, Workflow],
    sensors: list[Sensor],
    diags: DiagnosticCollector,
) -> None:
    """E215 — the `Github.` namespace is reserved for built-ins; a user may not declare an
    event, author a sensor, or have a step emit into it."""
    for evname, event in registry.events.items():
        if is_reserved(evname) and not event.builtin:
            diags.error(
                codes.E215,
                f"event '{evname}' uses the reserved '{GITHUB_PREFIX}' namespace "
                f"(built-in GitHub events); name it differently",
                span=event.span,
            )
    for sensor in sensors:
        if is_reserved(sensor.emits):
            diags.error(
                codes.E215,
                f"sensor '{sensor.name}' emits into the reserved '{GITHUB_PREFIX}' namespace; "
                f"built-in GitHub events are produced by the platform",
                span=sensor.span,
            )
    for workflow in workflows.values():
        for step in workflow.steps.values():
            for emitted in step.emits:
                if is_reserved(emitted):
                    diags.error(
                        codes.E215,
                        f"step '{step.id}' emits into the reserved '{GITHUB_PREFIX}' namespace; "
                        f"built-in GitHub events are produced by the platform",
                        span=step.span,
                    )
