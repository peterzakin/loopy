---
after: plan
agent: Worker
output:
  results: str
---
**Orchestrator-workers, step 2 of 3.**

> **Why one worker step, not N.** The cookbook spawns one worker *per* subtask, a count the
> orchestrator only knows at runtime. Loopy's DAG is static — the number of steps is fixed
> at compile time — so dynamic fan-out happens *inside* one worker step: this agent works
> the list sequentially in its own sandbox rather than the engine forking a step per item.
> (When the breakdown is known and fixed, prefer the `parallel/` shape — real parallel steps
> with an `after: [...]` join.)

Carry out each subtask in the plan, in order:

{{ plan.subtasks }}

For each, note what you did and the outcome. Return the consolidated `results`.
