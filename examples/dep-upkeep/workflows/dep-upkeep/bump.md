---
on: cron("0 3 * * *")
agent: Upkeeper
output:
  pr_url: url
  packages: int
emits: DepsBumped
budget: { wall_clock: 20, spend: { usd: 3 } }
---
A checkout of the repository is already in your workspace, with a GitHub token wired into git
and the `gh` CLI, so `git push` and PR creation just work.

Open one pull request that brings the project's dependencies up to date.

1. Detect the dependency manifests in the repo (for example `package.json`, `pyproject.toml`,
   `requirements.txt`, `go.mod`, `Cargo.toml`) and the ecosystem's way to find outdated or
   vulnerable packages.
2. Bump the outdated ones to their latest compatible versions. Prefer safe, in-range updates;
   note any major-version bumps separately rather than forcing them.
3. Create a branch `dep-upkeep/{{ event.scheduled_at }}`, commit the manifest (and lockfile)
   changes, push it, and open a PR against the default branch. The PR body should list each
   package with its old and new version, and flag any update that needs a human decision.

If nothing is outdated, do not open a PR. Return the PR URL (empty string if none) and the
number of packages bumped.
