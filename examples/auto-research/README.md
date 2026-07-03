# auto-research — a self-driving research loop

Inspired by Andrej Karpathy's "automated research" idea: instead of a human steering each
step, an agent loop keeps **reading new papers, forming a falsifiable hypothesis, running a
small experiment, writing up the result, and reflecting to pick the next thing to try** — and
then does it again. It's a research *loop*, which is exactly what Loopy is for.

Crucially, it assumes **no new Loopy features**. Every piece is something the other examples
already use:

```
arxiv_poll ─▶ PaperFound ─▶ digest ─▶ PaperDigest ─▶ ideate ─▶ Hypothesis ──┐
                                                                            ▼
   FindingLogged ◀─ writeup(report) ◀─ ExperimentDone ◀─ experiment(run) ◀──┘
                          │
                      reflect ─▶ Hypothesis ─▶ experiment ...   (the loop closes)
```

| Stage | Workflow | Agent | What it does |
|---|---|---|---|
| ingest | `sensors/arxiv_poll` | — | polls arXiv every 6h; **yields** one `PaperFound` per new paper |
| digest | `digest/read` | Researcher | distills a paper into testable claims/methods/results |
| ideate | `ideate/hypothesize` | Researcher | proposes one falsifiable hypothesis + the smallest experiment |
| experiment | `experiment/run` | Engineer | runs that experiment in the `Lab` sandbox, on a budget |
| writeup | `writeup/report` | Writer | logs an honest finding and opens a PR |
| reflect | `writeup/reflect` | Researcher | decides whether to spawn a follow-up — **closes the loop** |

## How the loop is built (and bounded)

The "auto" comes from one edge: `writeup/reflect` `emits: Hypothesis`, the same event
`experiment/run` triggers `on:`. So a finding can spawn the next experiment without a human in
the middle — `loopy compile` draws it as `ideate, writeup ─▶ Hypothesis ─▶ experiment`. This is
the **loop-back event** mechanism, the same one the evaluator-optimizer uses in
[`examples/effective-agents`](../effective-agents/).

A self-driving loop has to be *bounded*, and that's authored, not assumed:

- **A depth guard in the data.** Every `Hypothesis` carries `depth`; `ideate` sets it to 1 and
  `reflect` increments it, refusing to continue past depth 3. The chain from any one paper is
  finite.
- **A per-experiment budget.** `experiment/run` declares `budget: { wall_clock: 30, spend: {
  usd: 5 } }`, and the experiment-hygiene skill says to *shrink the question* rather than
  overrun — so each hop is cheap and decisive.
- **A reflect-or-stop decision in prose.** `reflect` emits a follow-up only when there's a
  genuinely new, falsifiable thread; otherwise it emits nothing and the branch ends. Loopy
  keeps that control flow in the agent, not in a DAG edge.

## What it would take to run for real

The example compiles and is safe to read as-is. To actually run it:

1. **A paper source.** `sensors/sensors.py` is a stub that emits nothing; the docstring
   sketches the real arXiv Atom API call. Or skip the sensor and fire a `PaperFound` by hand.
2. **A research-log repo.** Point both sandboxes' `repos:` at a repo you can push to (it's
   `you/research-log` in the registry) and wire git auth — see
   [`examples/codefix/`](../codefix/) for the `loopy auth github` flow.
3. **Secrets.** `cp base.env.example secrets/base.env` and fill in `ANTHROPIC_API_KEY` (the
   `Lab` sandbox reaches PyPI to fetch packages; egress is open by default, so nothing to wire).

```bash
uv run loopy compile examples/auto-research

# fire one paper by hand to walk the whole loop once
uv run loopy trigger manifest.json --root examples/auto-research \
  --event PaperFound \
  --fields '{"paper_id": "2401.00001", "title": "A Tiny Ablation Study", "url": "https://arxiv.org/abs/2401.00001", "abstract": "We show that ..."}'
```

> Running experiments is real work — prefer the `daytona` cloud sandbox (the default here) over
> `local`, and keep the depth guard and budgets in place before letting the loop run unattended.
