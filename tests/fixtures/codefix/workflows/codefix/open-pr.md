---
on: CodeTask
agent: Coder
output:
  pr_url: url
  summary: str
emits: PROpened
budget: { wall_clock: 15, spend: { usd: 2 } }
---
A checkout of the target repository is already in your workspace — the sandbox cloned it at
startup, and a GitHub token is wired into git, so `git push` and PR creation just work.

Make the change described by the task: {{ event.task }}.

Then:
1. Create a new branch named `{{ event.branch }}`.
2. Commit your edit with a clear message.
3. Push the branch and open a pull request against the default branch.

Return the pull request URL and a one-line summary of the change.
