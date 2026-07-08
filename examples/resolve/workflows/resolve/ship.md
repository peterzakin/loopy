---
after: [fix, review]
agent: Shipper
output:
  outcome: enum[merged, held]
---
The pull request {{ fix.pr_url }} came back from review with verdict: {{ review.verdict }}.

- If the verdict is `pass`, merge the pull request and return `outcome: merged`.
- If it is `fail`, leave the PR open, add a short comment saying it needs another round, and
  return `outcome: held`.

This step only merges or holds — make no other change.
