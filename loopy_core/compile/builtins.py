"""Inject built-in events + their producing sensors (Option A).

A workflow may trigger on a platform-shipped event (`on: Github.PullRequestOpened`,
`on: Sentry.IssueCreated`) without declaring it or authoring a sensor. This pass — run
after workflows/sensors are loaded and *before* `resolve_refs` and `cross_check` — does:

  1. Guards the reserved namespaces: a user-declared event or user sensor under any
     `BUILTIN_PROVIDERS` prefix is an error (E215). The platform owns producers there.
  2. For each built-in event a workflow triggers on, registers its contract (so
     `{{ event.field }}` refs resolve and the runtime can validate it) and synthesizes
     a built-in `Sensor` on the provider's webhook path (so the event has a producer —
     no W501).
  3. Reports an unknown reserved-prefix trigger (E112) with that provider's catalog.

Agents are never injected — every agent is declared explicitly in `registry.yml`
(`loopy init` scaffolds one per supported runtime); an unregistered `agent:` is E501.

Validation of the reserved namespaces lives entirely here; `cross_check` skips its
E504 registration check for reserved-prefix events (this pass owns them).
"""

from __future__ import annotations

from loopy_core.builtins import (
    is_reserved,
    provider_for,
)
from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.registry.model import Event, Registry
from loopy_core.registry.types import desugar
from loopy_core.sensors.model import Sensor, SensorTrigger
from loopy_core.span import Span, span_at
from loopy_core.workflow.model import Workflow

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
        provider = provider_for(name)
        if provider is None:  # unreachable: collection above is gated on is_reserved
            continue
        contract = provider.events.get(name)
        if contract is None:
            known = ", ".join(sorted(provider.events))
            diags.error(
                codes.E112,
                f"unknown built-in event '{name}'; known {provider.name} built-ins: {known}",
                span=referenced[name],
            )
            continue
        if name not in registry.events:
            registry.events[name] = _event(name, contract, diags)
        built.append(
            Sensor(
                name=f"builtin:{name}",
                trigger=SensorTrigger(
                    kind="webhook", path=provider.webhook_path, span=_BUILTIN_SPAN
                ),
                emits=name,
                source="builtin",
                provider=provider.name,
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
    """E215 — the built-in provider namespaces (`Github.`, `Sentry.`) are reserved; a user
    may not declare an event, author a sensor, or have a step emit into one."""
    for evname, event in registry.events.items():
        provider = provider_for(evname)
        if provider is not None and not event.builtin:
            diags.error(
                codes.E215,
                f"event '{evname}' uses the reserved '{provider.prefix}' namespace "
                f"(built-in {provider.name} events); name it differently",
                span=event.span,
            )
    for sensor in sensors:
        provider = provider_for(sensor.emits)
        if provider is not None:
            diags.error(
                codes.E215,
                f"sensor '{sensor.name}' emits into the reserved '{provider.prefix}' namespace; "
                f"built-in {provider.name} events are produced by the platform",
                span=sensor.span,
            )
    for workflow in workflows.values():
        for step in workflow.steps.values():
            for emitted in step.emits:
                provider = provider_for(emitted)
                if provider is not None:
                    diags.error(
                        codes.E215,
                        f"step '{step.id}' emits into the reserved '{provider.prefix}' "
                        f"namespace; built-in {provider.name} events are produced by the "
                        f"platform",
                        span=step.span,
                    )
