---
after: intake
agent: Reviewer
output:
  findings: str
---
**Parallelization — reviewer 1 of 3 (runs in parallel with `performance` and `style`).**

Review the change for **security** only: injection, authz/authn gaps, secret handling,
unsafe deserialization, SSRF, and unvalidated input.

Change: "{{ intake.summary }}" — diff at {{ intake.diff_url }}.

Return your security `findings` (or "none") — high-confidence issues only.
