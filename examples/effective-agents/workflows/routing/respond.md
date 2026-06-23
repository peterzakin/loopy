---
after: classify
agent: Writer
output:
  reply: str
---
**Routing, step 2 of 2.** Handle the query the classifier routed.

Query (from {{ event.customer }}): "{{ event.query }}"
Route: **{{ classify.route }}** — {{ classify.reason }}

> **Why this is one step, not four.** In the cookbook the router calls a different prompt
> per class. Loopy's DAG edges are static — there is no conditional `after:` — so the
> branch lives *in the agent*: pick the matching playbook below by `{{ classify.route }}`
> and follow only that one. (To route to genuinely separate runtimes or sandboxes instead,
> give each route its own handler step and let the agent no-op on routes that aren't its
> own — the same shape as the two GitHub sensors sharing one webhook.)

Playbooks:
- **billing** — Confirm the account, state the charge/refund policy plainly, and give the
  exact next action (e.g. where to change a plan). Never invent amounts.
- **technical** — Reproduce or pinpoint the problem, give a concrete fix or workaround, and
  link the relevant doc. Ask for the one missing detail if you can't yet diagnose it.
- **account** — Walk through the secure recovery/settings path. Never reset anything
  yourself; guide the customer through the verified flow.
- **general** — Answer directly and, if it actually belongs to another route, say which.

Write the customer-ready `reply`.
