---
on: Hypothesis
agent: Engineer
emits: ExperimentDone
output:
  metric: str
  result: str
  supported: enum[yes, no, inconclusive]
budget: { wall_clock: 30, spend: { usd: 5 } }
---
Test this hypothesis (hop {{ event.depth }} from {{ event.origin }}):

**{{ event.statement }}**

Plan: {{ event.experiment_plan }}

You're in the `Lab` sandbox — Python and pip are available. Following the experiment-hygiene
skill:
1. Write the smallest experiment that decides the hypothesis. Fix and record a random seed.
2. Run it. Keep it inside the wall-clock and spend budget above — if the honest experiment
   doesn't fit, shrink the scope (smaller model, fewer steps, a subset) rather than overrun.
3. Commit the experiment script and its logs to the research-log repo so the result is
   reproducible.

Emit an `ExperimentDone` reporting the `metric` measured, the `result` (number + setup +
seed), and whether the evidence `supported` the hypothesis (`yes`/`no`/`inconclusive`).
Carry `statement` and `depth` through unchanged. Report what you actually observed — a
refuted hypothesis is a real result, not a failure.
