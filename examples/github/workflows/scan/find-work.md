---
on: Github.PullRequestMerged
agent: Scout
output:
  ideas: str
emits: WorkProposed
budget: { wall_clock: 15, spend: { usd: 2 } }
---
Pull request #{{ event.number }} — "{{ event.title }}" — was just merged into
{{ event.repo }} by {{ event.merged_by }}. A checkout of the repo is in your workspace.

Mine this change for the follow-on work it implies:

1. Read what the PR changed (inspect its merge commit / diff).
2. Look for natural next steps it opened up: TODOs or `FIXME`s it left behind, code paths it
   added without tests, docs or comments it made stale, and adjacent code that should change
   for consistency now that this landed.
3. Produce a concise, prioritized list of 3–5 concrete tasks. For each: a one-line title and
   a one-sentence rationale grounded in something specific from the diff.

Return the list as the `ideas` output. (A downstream workflow could subscribe to the
`WorkProposed` event to file these as issues — out of scope here.)
