# grading-rubric

A reusable rubric for the agents that act as a judge — the parallel `aggregate` step and the
evaluator-optimizer's `evaluate` step. The point of a written rubric is that grading is
*consistent* across rounds and across reviewers, so the optimizer loop converges instead of
chasing a moving target.

Grade against these dimensions, in order — a failure high on the list outranks polish lower
down:

1. **Correct** — Does it actually satisfy the brief, with no false claims? A confident wrong
   answer fails outright.
2. **Complete** — Are all parts of the ask covered? Note anything missing by name.
3. **Grounded** — Are specifics (numbers, names, APIs) supported by the input, not invented?
4. **Clear** — Could the intended reader act on it without re-reading? Flag ambiguity.
5. **Appropriately scoped** — No padding, no unrequested scope; matches the asked-for length
   and altitude.

Rules for the verdict:

- **Pass only when 1–3 are fully met.** Clarity and scope issues alone warrant `fail` with
  feedback only if they'd block the reader.
- **Feedback must be actionable.** Quote the offending span and say what to change — "tighten
  the intro" is useless; "the second sentence claims 40% but the input says 25% — correct it"
  is not.
- **Don't move the goalposts.** Only raise issues that were in scope on round one; new asks
  belong in a new brief, not a revision note.
- **Be decisive.** When it clears the bar, pass and stop — over-grading wastes loop rounds.
