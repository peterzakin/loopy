---
on: cron("0 9 * * 1-5")
agent: Digester
output:
  markdown: str
  pr_count: int
emits: DigestReady
budget: { wall_clock: 10, spend: { usd: 1 } }
---
A checkout of the repository is already in your workspace, with a GitHub token wired into
git and the `gh` CLI, so reading the repo's history just works.

Write a short standup digest of what merged since the last run. The window is everything
between `{{ event.last_run }}` and `{{ event.scheduled_at }}` (on the very first run,
`last_run` is one interval back).

1. List the pull requests merged in that window (use `gh pr list --state merged --search
   "merged:>={{ event.last_run }}"`, or the merge commits on the default branch).
2. For each, write one line: the PR title, its author, and a one-clause summary of what it
   changed, grounded in the diff, not just the title.
3. Group by theme (features, fixes, chores) if there's more than a handful. If nothing
   merged, say so in one line.

Return the digest as `markdown` and the number of merged PRs as `pr_count`.
