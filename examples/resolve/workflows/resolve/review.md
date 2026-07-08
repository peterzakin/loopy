---
after: fix
agent: Reviewer
output:
  verdict: enum[pass, fail]
---
Review the pull request at {{ fix.pr_url }}.

The author's summary of the change: {{ fix.summary }}

Read the diff and decide whether it is correct, focused, and tested where a test is
warranted.

Return `pass` if it is ready to merge as-is, or `fail` if it needs another round.
