---
after: intake
agent: Reviewer
output:
  findings: str
---
**Parallelization — reviewer 2 of 3 (runs in parallel with `security` and `style`).**

Review the change for **performance** only: N+1 queries, accidental quadratic loops,
unbounded allocations, missing pagination/limits, and needless work on the hot path.

Change: "{{ intake.summary }}" — diff at {{ intake.diff_url }}.

Return your performance `findings` (or "none") — high-confidence issues only.
