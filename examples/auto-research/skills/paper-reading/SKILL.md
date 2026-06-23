# paper-reading

How to turn a paper into a digest the rest of the loop can act on. The goal is a faithful,
testable distillation — not a summary that sounds impressive.

- **Separate claim from evidence.** State what the paper *asserts* apart from what it
  *showed*. "Method X improves Y" is a claim; "X beat the baseline by 2.1 points on Z at 7B
  scale, one seed" is the evidence. The ideate step needs both to find a real follow-up.
- **Find the load-bearing experiment.** Which single result, if it didn't replicate, would
  undercut the paper? That's the one worth re-testing first.
- **Note the regime.** Scale, dataset, compute, and seeds the result depends on — follow-ups
  usually live in a regime the authors *didn't* test.
- **Record what's missing.** Skipped ablations, unreported variance, and untested edge cases
  are the richest source of cheap, decisive experiments.
- **Don't inflate.** If the paper shows a narrow effect, say so. A hypothesis built on an
  overstated claim wastes an experiment.
