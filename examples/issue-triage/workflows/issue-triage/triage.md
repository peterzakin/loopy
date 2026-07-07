---
on: Github.IssueOpened
agent: Triager
output:
  area: enum[bug, feature, docs, question, security]
  severity: enum[low, medium, high, critical]
  comment_url: url
emits: IssueTriaged
budget: { wall_clock: 15, spend: { usd: 2 } }
---
Issue #{{ event.number }} ("{{ event.title }}") was opened by {{ event.author }}:

{{ event.body }}

1. Read it against the code in the checkout.
2. Classify one `area` and one `severity` (security is at least high).
3. Post one comment on {{ event.url }} via `gh`: area, severity, and a single concrete
   next step. Be brief.

Return the area, the severity, and the comment URL.
