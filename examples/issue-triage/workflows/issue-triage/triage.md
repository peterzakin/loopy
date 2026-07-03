---
on: Github.IssueOpened
agent: Triager
output:
  area: enum[bug, feature, docs, question, security]
  severity: enum[low, medium, high, critical]
  comment_url: url
emits: IssueTriaged
budget: { wall_clock: 10, spend: { usd: 1 } }
---
A checkout of {{ event.repo }} is in your workspace, with a GitHub token wired into the
`gh` CLI, so posting a comment just works.

Issue #{{ event.number }} — "{{ event.title }}" — was just opened by {{ event.author }}:

{{ event.body }}

Triage it:

1. Read the issue against the code in the checkout to ground your judgment.
2. Classify it into one `area` (bug, feature, docs, question, security) and one `severity`
   (low, medium, high, critical). Weigh user impact and blast radius; treat anything
   security-relevant as at least high.
3. Post one comment on {{ event.url }} via `gh` stating the area, the severity, and a single
   concrete next step (what info is missing, or where in the code to start). Be brief and
   specific. Do not restate the whole issue.

Return the chosen `area` and `severity`, and the URL of the comment you posted.
