---
after: generate
agent: Judge
output:
  verdict: enum[pass, fail]
  feedback: str
emits: ContentRequested
---
**Evaluator-optimizer, step 2 of 2 (the loop).** Grade the draft against the brief using
the grading-rubric skill:

Brief: {{ event.brief }}
Draft: {{ generate.draft }}

- If it meets the bar, set `verdict: pass` and **stop** — do not emit anything; the loop ends.
- If it falls short, set `verdict: fail`, write specific, actionable `feedback`, and **emit
  `ContentRequested`** carrying the *same* `brief` and your `feedback`. That re-enters this
  workflow at `generate` for another round.

> **How the loop is built.** This step both `emits: ContentRequested` and the workflow
> triggers `on: ContentRequested` — a loop-back on the bus, the documented way to send work
> back to a workflow's entry. The stop condition is in this prose (emit only on `fail`), not
> a DAG edge, because Loopy keeps control flow in the agent. Keep a guard in the brief (e.g.
> "stop after 3 rounds") so the loop always terminates.
