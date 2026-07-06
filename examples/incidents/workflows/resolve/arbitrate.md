---
on: WorkItem
agent: Investigator
output:
  goal: str
---
Decide the goal for "{{ event.description }}" given the work item at {{ event.link }}.
