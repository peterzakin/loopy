"""Budget enforcement (B6) — wall_clock (minutes) and spend.usd. window/latency are
day-scale and belong to the deferred durable-timer work."""

from __future__ import annotations

from collections.abc import Mapping

from loopy_runtime.manifest_model import BudgetSpec


class BudgetExceeded(Exception):
    pass


class CascadeBudgetExceeded(BudgetExceeded):
    """The cumulative spend of a cascade reached its USD cap. Raised by `CascadeBudget.check`
    before a step runs; recorded as a failed run (not an engine crash). Rationale lives on
    `CascadeBudget` below."""


class WorkflowBudgetExceeded(BudgetExceeded):
    """Like CascadeBudgetExceeded but scoped to one named workflow's steps, from registry
    `limits.workflows.<name>.spend`."""


class CascadeBudget:
    """Cascade-scoped USD accounting: the project-wide cascade cap plus the per-workflow caps
    from registry `limits.workflows`, with accumulators that live for one cascade. The runtime
    resets it per drain; the WorkflowRunner calls `check` before each step and `add` after.

    The terminator for a runaway loop-back cascade: each step stays within its per-step budget
    while cumulative cost grows unbounded (`max_iterations` is a count, not a budget) — these
    caps bound the sum. Dollars are the unit because cost is the legible budget; it's only
    enforceable when every reachable agent uses a cost-reporting harness, gated all-or-nothing
    in `preflight()` and backstopped in `add`. See the cost-budget plan."""

    def __init__(self, cascade_cap: float | None, workflow_caps: Mapping[str, float]) -> None:
        self.cascade_cap = cascade_cap
        self.workflow_caps = dict(workflow_caps)
        self._cascade_cost = 0.0
        self._workflow_cost: dict[str, float] = {}

    def reset(self) -> None:
        """Start a new cascade: zero both accumulators (the caps are configuration and stay)."""
        self._cascade_cost = 0.0
        self._workflow_cost = {}

    def applies_to(self, wf_name: str) -> bool:
        """Whether any dollar cap (the cascade-wide one or this workflow's own) covers a step of
        `wf_name` — the gate for cost accumulation and for preflight's cost-capability check."""
        return self.cascade_cap is not None or wf_name in self.workflow_caps

    def check(self, wf_name: str, step_id: str) -> None:
        """Raise if a dollar cap — the cascade cap or this workflow's own — has already been
        reached. Checked before each step so an over-budget run never starts the agent → it
        emits nothing → the loop-back winds down on its own."""
        if self.cascade_cap is not None and self._cascade_cost >= self.cascade_cap:
            raise CascadeBudgetExceeded(
                f"cascade spent ${self._cascade_cost:.4f}, reaching the cap of "
                f"${self.cascade_cap} before step '{step_id}'"
            )
        wf_cap = self.workflow_caps.get(wf_name)
        if wf_cap is not None and self._workflow_cost.get(wf_name, 0.0) >= wf_cap:
            raise WorkflowBudgetExceeded(
                f"workflow '{wf_name}' spent ${self._workflow_cost.get(wf_name, 0.0):.4f}, "
                f"reaching its cap of ${wf_cap} before step '{step_id}'"
            )

    def add(self, wf_name: str, step_id: str, usage) -> None:  # noqa: ANN001 - Usage
        """Add a step's USD cost to the cascade total and its workflow's total when a dollar
        cap touches it. A `cost_usd is None` under an active cap is a hard error, never
        counted as $0 — the backstop for a harness that reports cost in general but returned
        None for this call. No-op when no cap applies to this step."""
        if not self.applies_to(wf_name):
            return
        if usage.cost_usd is None:
            raise RuntimeError(
                f"step '{step_id}' ran under an active spend cap but its harness reported "
                "no USD cost for this call, so the cap can't be enforced (never counted as $0) — "
                "use a harness/provider that reports cost."
            )
        if self.cascade_cap is not None:
            self._cascade_cost += usage.cost_usd
        if wf_name in self.workflow_caps:
            self._workflow_cost[wf_name] = self._workflow_cost.get(wf_name, 0.0) + usage.cost_usd


class BudgetEnforcer:
    @staticmethod
    def wall_clock_seconds(budget: BudgetSpec | None) -> float | None:
        if budget and budget.wall_clock:
            return budget.wall_clock * 60
        return None

    @staticmethod
    def check_spend(budget: BudgetSpec | None, spent_usd: float) -> None:
        if not budget or not budget.spend:
            return
        limit = budget.spend.get("usd")
        if limit is not None and spent_usd > limit:
            raise BudgetExceeded(f"spend ${spent_usd:.4f} exceeds budget ${limit}")
