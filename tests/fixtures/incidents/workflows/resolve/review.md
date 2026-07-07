---
after: fix
agent: Reviewer
output:
  verdict: enum[pass, fail]
---
Review {{ fix.pr_url }}: {{ fix.summary }}. Return a pass/fail verdict.
