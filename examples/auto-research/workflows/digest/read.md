---
on: PaperFound
agent: Researcher
emits: PaperDigest
output:
  claims: str
  methods: str
  results: str
---
Read paper {{ event.paper_id }} — "{{ event.title }}" ({{ event.url }}).

Abstract: {{ event.abstract }}

Using the paper-reading skill, distill it into a structured digest the rest of the loop can
build on:
- `claims` — the key claims, stated plainly enough to be testable.
- `methods` — how the authors tested them (datasets, scale, ablations).
- `results` — what they actually found, with the numbers that matter.

Emit a `PaperDigest` carrying these plus the `paper_id` and `title`. Be faithful to the
paper — do not embellish a claim into something the authors didn't show.
