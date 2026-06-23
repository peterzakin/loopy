---
on: ContentRequested
agent: Writer
output:
  draft: str
---
**Evaluator-optimizer, step 1 of 2.** One agent produces, another grades, and the work
loops until it's good enough — the optimizer half of the loop.

Produce content for this brief:

{{ event.brief }}

If `feedback` is present, this is a revision — fix exactly what it calls out and don't
regress what already worked:

{{ event.feedback }}

Return the `draft`.
