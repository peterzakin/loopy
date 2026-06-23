---
on: FeatureRequest
agent: Orchestrator
output:
  subtasks: str
---
**Orchestrator-workers, step 1 of 3.** Unlike parallelization's *fixed* sections, the
orchestrator decides the breakdown at runtime — it reads the request and decomposes it into
however many subtasks it actually needs.

Decompose this request into an ordered list of concrete subtasks:

"{{ event.request }}"

For each subtask give a one-line title and a one-sentence outcome. Return the list as the
`subtasks` output — the worker step executes it next.
