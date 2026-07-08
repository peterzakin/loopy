---
on: WorkItem
agent: Arbiter
output:
  goal: str
---
A work item came in: "{{ event.description }}"

The full item is at {{ event.link }}, and a checkout of the repo is in your workspace.

Turn it into one concrete goal an engineer could act on today:

1. Read the work item and skim the code it points at.
2. Decide the single most useful change it implies. If the ask is broad, narrow it to the
   smallest version that still delivers the value.

Return that as `goal`: one sentence, specific enough that the next step can implement it
without re-reading the work item.
