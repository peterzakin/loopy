---
on: cron("0 3 * * *")
agent: Upkeeper
output:
  pr_url: url
  packages: int
emits: DepsBumped
budget: { wall_clock: 30, spend: { usd: 5 } }
---
A checkout is in your workspace, with a GitHub token wired into git and the `gh` CLI, so
`git push` and PR creation just work.

Open one PR that brings dependencies up to date:

1. Detect the manifests (package.json, pyproject.toml, go.mod, ...).
2. Bump outdated packages to latest compatible versions; flag major-version bumps for a
   human rather than forcing them.
3. Branch (name it after this run, e.g. from {{ event.scheduled_at }}), commit, push, and
   open a PR listing old and new versions.

If nothing is outdated, open no PR. Return the URL (empty if you skipped) and the count.
