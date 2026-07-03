---
on: Incident
agent: Responder
output:
  issue_url: url
emits: Acknowledged
budget: { wall_clock: 8, spend: { usd: 1 } }
---
The health check for {{ event.url }} returned status {{ event.status }} instead of 200, so
the endpoint looks down. A checkout of the repo is in your workspace with a GitHub token
wired into the `gh` CLI.

Open a GitHub issue for the outage:

1. Before filing, check open issues (`gh issue list`) for an existing, still-open outage
   report for this URL. If one exists, add a brief comment noting the check is still failing
   instead of opening a duplicate.
2. Otherwise open an issue titled for the outage, with the URL, the observed status, and the
   time in the body. Label it if the repo has a fitting label.

Return the URL of the issue you opened (or the existing one you updated) as `issue_url`.
