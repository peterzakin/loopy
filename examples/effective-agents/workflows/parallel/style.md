---
after: intake
agent: Reviewer
output:
  findings: str
---
**Parallelization — reviewer 3 of 3 (runs in parallel with `security` and `performance`).**

Review the change for **clarity and maintainability** only: naming, dead code, missing
tests for new branches, and consistency with surrounding conventions. Don't nitpick
formatting a linter would catch.

Change: "{{ intake.summary }}" — diff at {{ intake.diff_url }}.

Return your maintainability `findings` (or "none").
