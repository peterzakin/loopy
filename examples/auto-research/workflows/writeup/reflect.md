---
after: report
agent: Researcher
emits: Hypothesis
---
This is the hinge that makes the loop *auto*: decide whether the finding just logged opens a
follow-up worth running.

Finding: {{ report.conclusion }} ({{ report.report_url }})
Result tested: "{{ event.statement }}" → supported: {{ event.supported }} (hop {{ event.depth }})

Decide, and act:
- **Stop** when the thread is exhausted, the result was inconclusive for boring reasons, or
  the depth guard is hit — **emit nothing**, and the branch ends here.
- **Continue** only with a genuinely new, falsifiable follow-up (a result that surprised you,
  a confound to rule out, the next scale up). Emit a `Hypothesis` whose `origin` references
  this finding, with a fresh `experiment_plan`, and **`depth` set to `{{ event.depth }} + 1`**.

> **Loop guard — don't skip it.** This step `emits: Hypothesis`, the same event
> `experiment/run` triggers `on:`, so emitting re-enters the loop (`writeup ─▶ Hypothesis ─▶
> experiment`). Refuse to continue past **depth 3**, and only continue when the follow-up is
> truly new — that, plus the per-experiment budget in `experiment/run`, is what keeps the
> research loop from running forever.
