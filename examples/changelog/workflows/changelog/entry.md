---
on: Github.PullRequestMerged
agent: Scribe
output:
  pr_url: url
  entry: str
emits: ChangelogUpdated
budget: { wall_clock: 15, spend: { usd: 2 } }
---
PR #{{ event.number }} ("{{ event.title }}") was merged by {{ event.merged_by }}. Record
it in the changelog.

1. Read what the PR changed (its merge commit / diff).
2. Write one changelog line in the repo's existing style; create CHANGELOG.md if there
   isn't one. Skip purely internal changes.
3. Branch, commit, push, and open a PR against the default branch.

Return the PR URL (empty if skipped) and the entry text.
