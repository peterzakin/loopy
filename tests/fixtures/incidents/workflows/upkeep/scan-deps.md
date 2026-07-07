---
on: cron("0 3 * * *")
agent: Investigator
emits: WorkItem
---
Scan dependencies changed since {{ event.last_run }} and open WorkItems for any risks.
