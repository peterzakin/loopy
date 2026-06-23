---
on: SupportQuery
agent: Classifier
output:
  route: enum[billing, technical, account, general]
  reason: str
---
**Routing, step 1 of 2.** Routing sends each input to the handler best suited to it,
so a specialist prompt can do better than one prompt that tries to cover everything.

Classify this support query from {{ event.customer }}:

"{{ event.query }}"

Pick exactly one `route`:
- `billing` — charges, invoices, refunds, plan changes
- `technical` — errors, bugs, integration and how-to-fix questions
- `account` — login, access, security, profile and org settings
- `general` — anything else, including unclear queries

Return the chosen `route` and a one-line `reason`. The next step handles the query using
the playbook for whichever route you pick.
