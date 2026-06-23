---
after: [security, performance, style]
agent: Judge
output:
  verdict: enum[approve, request_changes]
  report: str
---
**Parallelization, step 3 of 3 (aggregate).** The `after: [security, performance, style]`
fan-in is what makes the three reviews run in parallel and join here once all three finish.

Merge the three independent reviews into one decision, using the grading-rubric skill:
- Security: {{ security.findings }}
- Performance: {{ performance.findings }}
- Maintainability: {{ style.findings }}

De-duplicate overlapping points, order by severity, and decide `verdict`:
`request_changes` if any reviewer raised a real blocker, else `approve`. Return a short
combined `report`.
