---
on: ChangeSubmitted
agent: Writer
output:
  summary: str
  diff_url: url
---
**Parallelization, step 1 of 3 (sectioning).** Parallelization runs independent subtasks
at the same time and combines the results — here, three reviewers each look at the *same*
change through a different lens, then an aggregator merges their findings.

This step just carries the change forward so the three reviewers can each read it:
"{{ event.summary }}" — diff at {{ event.diff_url }}.

Return the `summary` and `diff_url` unchanged.
