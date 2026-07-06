---
on: Incident
agent: Responder
output:
  issue_url: url
emits: Acknowledged
budget: { wall_clock: 10, spend: { usd: 1 } }
---
The health check for {{ event.url }} returned {{ event.status }} instead of 200. Open a
GitHub issue for the outage (and skip it if an open one already exists). Return the
issue URL.
