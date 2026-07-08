---
after: arbitrate
agent: Fixer
output:
  pr_url: url
  summary: str
budget: { wall_clock: 20, spend: { usd: 4 } }
---
Implement this goal:

> {{ arbitrate.goal }}

1. Make the change on a new branch.
2. Keep it tight — just what the goal asks for, plus a test if the change warrants one.
3. Open a pull request against the default branch.

Return the pull request URL as `pr_url` and a one-line `summary` of what you changed.
