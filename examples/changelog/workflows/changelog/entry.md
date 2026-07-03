---
on: Github.PullRequestMerged
agent: Scribe
output:
  pr_url: url
  entry: str
emits: ChangelogUpdated
budget: { wall_clock: 10, spend: { usd: 1 } }
---
A checkout of {{ event.repo }} is in your workspace, with a GitHub token wired into git and
the `gh` CLI, so `git push` and PR creation just work.

Pull request #{{ event.number }} — "{{ event.title }}" — was just merged by
{{ event.merged_by }}. Record it in the changelog.

1. Read what the PR changed (inspect its merge commit / diff at {{ event.url }}).
2. Write one changelog line in the style the repo already uses. If there's a `CHANGELOG.md`,
   match its format and add the entry under the unreleased/latest section (create that
   section if it's missing). If there's no changelog, create `CHANGELOG.md` with a
   "Keep a Changelog"-style header and this first entry. Skip purely internal changes
   (formatting, CI) that don't belong in a user-facing changelog, and if so, open no PR.
3. Create a branch `changelog/pr-{{ event.number }}`, commit the change, push, and open a PR
   against the default branch.

Return the PR URL (empty string if you skipped) and the changelog `entry` text you wrote.
