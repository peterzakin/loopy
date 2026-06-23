---
on: ExperimentDone
agent: Writer
emits: FindingLogged
output:
  report_url: url
  conclusion: str
---
Write up the result of testing: **{{ event.statement }}**

- metric: {{ event.metric }}
- result: {{ event.result }}
- supported: {{ event.supported }}

A checkout of the research-log repo is in your workspace. Add a short, dated entry under
`findings/`:
- State the hypothesis, what was measured, and the outcome in plain language.
- Link the experiment script and logs the Engineer committed.
- Be honest about limits — single seed, small scale, what would be needed to trust it more.

Commit and open a PR. Emit `FindingLogged` with the PR `report_url` and a one-line
`conclusion`. The `reflect` step then decides whether this opens a follow-up worth chasing.
