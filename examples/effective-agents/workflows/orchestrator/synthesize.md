---
after: work
agent: Orchestrator
output:
  deliverable: str
---
**Orchestrator-workers, step 3 of 3.** The orchestrator returns to stitch the workers'
outputs into one coherent result.

Original request: "{{ event.request }}"
Worker results: {{ work.results }}

Synthesize a single, coherent `deliverable` that satisfies the original request — resolve
any inconsistencies between subtask outputs rather than concatenating them.
