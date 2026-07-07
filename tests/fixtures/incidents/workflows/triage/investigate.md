---
on: Incident
agent: Investigator
emits: WorkItem
---
Triage incident {{ event.issue_id }} — "{{ event.title }}" ({{ event.link }}).
Find the root cause and normalize it into a WorkItem for the resolve loop.
