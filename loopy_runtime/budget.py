"""Budget enforcement (B6) — wall_clock (minutes) and spend.usd. window/latency are
day-scale and belong to the deferred durable-timer work."""

from __future__ import annotations

from loopy_runtime.manifest_model import BudgetSpec


class BudgetExceeded(Exception):
    pass


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
