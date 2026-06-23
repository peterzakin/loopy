# experiment-hygiene

Rules for running experiments inside a single sandbox under a tight budget. A small, honest
experiment that actually finishes beats an ambitious one that times out or can't be trusted.

- **Smallest decisive experiment.** Shrink until it fits the step's wall-clock and spend
  budget: a tiny model, few steps, a data subset. Scale is something a *later* hop earns
  after a small version shows signal — not something to reach for up front.
- **Fix the seed, and say so.** Set and record every random seed. A result without a seed
  isn't reproducible and shouldn't be reported as one.
- **One variable.** Change exactly the thing the hypothesis is about; hold the rest constant
  against a baseline run. A difference you can't attribute is not a result.
- **Log enough to reproduce.** Commit the script, the exact command, the seed, and the raw
  output to the research-log repo. The writeup links these — claims without artifacts don't
  count.
- **Report what happened.** State the number with its setup, and call inconclusive results
  inconclusive. A refuted hypothesis is a clean negative result; do not massage it into a
  win.
- **Stay in budget honestly.** If the real experiment won't fit, narrow the question — never
  fake or extrapolate a number to fit the time.
