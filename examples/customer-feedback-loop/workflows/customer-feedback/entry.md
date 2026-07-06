---
on: CustomerTicket
agent: SupportEngineer
output:
  pr_url: url
  verdict: str
budget: { wall_clock: 20, spend: { usd: 3 } }
---
A customer opened Zendesk ticket {{ event.ticket_id }}: "{{ event.subject }}".

{{ event.body }}

Decide whether this ticket points at real work in this codebase.

1. Search the code for the behavior the ticket describes.
2. If it is a bug or a small feature you can address, implement the change on a branch
   and open a pull request that links back to {{ event.link }}. If it is a question, a
   duplicate, or not actionable in code, do nothing.
3. Return the PR URL (empty if you skipped) and a one-line verdict.
