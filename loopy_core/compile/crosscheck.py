"""P8 — whole-IR cross-cutting checks + lineage (X5).

X2 binds step agents and agent sandbox/skills; X3 checks event registration on
on:/emits:; X5 builds the typed lineage graph and warns on dead triggers (W501).
"""

from __future__ import annotations

from collections import defaultdict

from loopy_core.builtins import is_reserved
from loopy_core.compile import codes
from loopy_core.compile.diagnostics import DiagnosticCollector
from loopy_core.compile.model import EventLineage, Lineage, Project
from loopy_core.discovery import Inventory
from loopy_core.span import span_at


def cross_check(project: Project, inv: Inventory, diags: DiagnosticCollector) -> None:
    registry = project.registry

    # X2 — step agents resolve; X3 — step on:/emits: events registered.
    for workflow in project.workflows.values():
        for step in workflow.steps.values():
            if step.agent is not None and step.agent not in registry.agents:
                diags.error(
                    codes.E501,
                    f"step '{step.id}' uses agent '{step.agent}', which is not registered",
                    span=step.span,
                )
            for event in step.emits:
                # Reserved-namespace events are validated by the built-in pass (E215/E112).
                if event not in registry.events and not is_reserved(event):
                    diags.error(
                        codes.E504,
                        f"step '{step.id}' emits unregistered event '{event}'",
                        span=step.span,
                    )
            if step.trigger is not None and step.trigger.kind == "event":
                event = step.trigger.event
                if event not in registry.events and not is_reserved(event):
                    diags.error(
                        codes.E504,
                        f"step '{step.id}' triggers on unregistered event '{event}'",
                        span=step.span,
                    )

    # X2 — every registered agent names a sandbox, and it (+ its skills) resolve.
    for agent in registry.agents.values():
        # Both `model:` and `harness:` are mandatory flat keys (directly or via
        # `defaults.agent`) — neither is ever inferred from the other.
        if agent.model is None:
            diags.error(
                codes.E507,
                f"agent '{agent.name}' declares no model; every agent must name one "
                f"(set model: on the agent or defaults.agent.model)",
                span=agent.span,
            )
        if agent.harness is None:
            diags.error(
                codes.E507,
                f"agent '{agent.name}' declares no harness; every agent must name one "
                f"(set harness: on the agent or defaults.agent.harness)",
                span=agent.span,
            )
        if agent.sandbox is None:
            # Where an agent runs is never inferred — it must name a sandbox (directly or via
            # `defaults.agent.sandbox`). Pairs with E214 (that sandbox must declare a provider).
            diags.error(
                codes.E506,
                f"agent '{agent.name}' declares no sandbox; every agent must name one "
                f"(set sandbox: on the agent or defaults.agent.sandbox)",
                span=agent.span,
            )
        elif agent.sandbox not in registry.sandboxes:
            diags.error(
                codes.E502,
                f"agent '{agent.name}' references sandbox '{agent.sandbox}', "
                f"which is not registered",
                span=agent.span,
            )
        for skill in agent.skills:
            if skill not in inv.skill_names:
                diags.error(
                    codes.E503,
                    f"agent '{agent.name}' references skill '{skill}', which is not in skills/",
                    span=agent.span,
                )

    # X2 — every workflow a per-workflow spend limit names must exist (workflows weren't loaded
    # when the registry parsed `limits`, so this resolves here, like the agent/sandbox checks).
    if registry.limits is not None:
        for wf_name in registry.limits.workflows:
            if wf_name not in project.workflows:
                diags.error(
                    codes.E505,
                    f"registry limits.workflows names '{wf_name}', which is not a workflow",
                    span=span_at("registry.yml"),
                )

    # X5 — lineage + dead-trigger warnings.
    project.lineage = _build_lineage(project, diags)


def _build_lineage(project: Project, diags: DiagnosticCollector) -> Lineage:
    producers: dict[str, set[str]] = defaultdict(set)
    consumers: dict[str, set[str]] = defaultdict(set)

    for sensor in project.sensors:
        producers[sensor.emits].add(sensor.name)

    for workflow in project.workflows.values():
        for step in workflow.steps.values():
            for event in step.emits:
                producers[event].add(step.id)
            if step.trigger is not None and step.trigger.kind == "event":
                consumers[step.trigger.event].add(step.id)

    events = sorted(set(producers) | set(consumers))
    lineage = Lineage(
        events={
            name: EventLineage(
                producers=sorted(producers.get(name, set())),
                consumers=sorted(consumers.get(name, set())),
            )
            for name in events
        }
    )

    # W5 / X5 — a registered event used as an on: trigger with no producer is dead.
    for event in sorted(consumers):
        if event in project.registry.events and not producers.get(event):
            diags.warning(
                codes.W501,
                f"dead trigger: event '{event}' is consumed by an on: step but has no producer",
            )

    return lineage
