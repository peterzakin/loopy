---
on: PaperDigest
agent: Researcher
emits: Hypothesis
output:
  statement: str
  experiment_plan: str
---
From the digest of "{{ event.title }}" ({{ event.paper_id }}), propose one hypothesis worth
testing:
- claims: {{ event.claims }}
- methods: {{ event.methods }}
- results: {{ event.results }}

Pick the single most interesting, *falsifiable* follow-up — a smaller-scale replication, an
ablation the authors skipped, or a claim that might not hold in a regime they didn't test.

Emit a `Hypothesis` with:
- `statement` — one sentence, falsifiable.
- `experiment_plan` — the **smallest** experiment that would support or refute it, runnable
  in a single sandbox in minutes (see the experiment-hygiene skill for what "small" means).
- `origin` — set to `{{ event.paper_id }}`.
- `depth` — set to `1` (this is the first hop from the paper; the loop increments it).
