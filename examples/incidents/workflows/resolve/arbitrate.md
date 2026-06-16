---
on: WorkItem
agent: Investigator
output:
  goal: str
---
Decide the goal for "{{ event.proposed_goal }}" given the work item at {{ event.link }}.
