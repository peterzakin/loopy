# resolve — a multi-step workflow with the `after:` chain

Every other example in this cookbook is a **single step**. This one is the smallest look at a
workflow with **more than one**: four steps wired in a row with `after:`, passing typed
outputs down the chain.

One work item comes in, and four agents take turns on it:

| Step | Wiring | Agent | Reads | Returns |
| --- | --- | --- | --- | --- |
| `arbitrate` | `on: WorkItem` | Arbiter | `event.description`, `event.link` | `goal` |
| `fix` | `after: arbitrate` | Fixer | `arbitrate.goal` | `pr_url`, `summary` |
| `review` | `after: fix` | Reviewer | `fix.pr_url`, `fix.summary` | `verdict` |
| `ship` | `after: [fix, review]` | Shipper | `fix.pr_url`, `review.verdict` | `outcome` |

The compiler builds the DAG straight from those `after:` links:

```
  ⚡  resolve
      ◇ on event WorkItem
      │
      ▼
      ● arbitrate  🤖 Arbiter
      │
      ▼
      ● fix        🤖 Fixer
      │
      ▼
      ● review     🤖 Reviewer
      │
      ▼
      ● ship       🤖 Shipper  ·  after fix, review
```

## The `after:` syntax

**One entry step, everything else `after:` something.** Exactly one step carries `on:` (here
`on: WorkItem`) — that's the root. Every other step carries `after:` naming the step(s) it
runs behind. A step with neither is an orphan and fails the compile.

**Two forms.** `after:` takes a single step (`after: arbitrate`) or a list
(`after: [fix, review]`). `ship` uses the list form because it needs two things: the PR to
merge, which came from `fix`, and the go/no-go, which came from `review`. Its two `after:`
edges are exactly those two data dependencies.

**Data flows by reference, down the chain.** A step reads a predecessor's output with
`{{ step.field }}` — `fix` reads `{{ arbitrate.goal }}`, `review` reads `{{ fix.pr_url }}`. The
triggering event is `{{ event.field }}`. These are **outputs**: they stay inside the workflow
and never touch the event bus.

**You can only reference a _direct_ `after:` predecessor.** `review` is `after: fix`, so it can
read `fix.*` but **not** `arbitrate.*` — even though `arbitrate` ran earlier. If a step needs
an earlier step's output, add that step to its `after:` list (which is why `ship` lists `fix`,
not just `review`). Reach past a direct predecessor and the compile fails with `LOOPY-E304`.

## Outputs vs. events

Nothing in this workflow is `emits:`ed. The whole handoff — goal → PR → verdict → merge — is
**outputs** passed along `after:` edges, private to the chain. That's the rule of thumb: a
handoff *within* a workflow is an output; a handoff *between* workflows is an event. If you
wanted another workflow to react once a change ships, `ship` would `emits:` a registered event
and that workflow would trigger `on:` it.

## Run it

`WorkItem` has no sensor in this project, so fire one by hand. `loopy trigger` runs the whole
chain in-memory:

```
loopy compile examples/resolve --check     # validate the DAG, refs, and templates
loopy trigger examples/resolve \
  --event WorkItem \
  --fields '{"link":"https://github.com/octocat/Hello-World/issues/1","description":"Typos in the README break the quickstart copy-paste"}' \
  --json                                    # full run record: each step, its outputs, the final result
```

Before a real run, give the sandbox its credentials: `cp base.env.example secrets/base.env`
and fill in `ANTHROPIC_API_KEY` (and, without a GitHub App, a `GITHUB_TOKEN` — the fixer opens
a PR and the shipper merges one).

> Compiling prints `LOOPY-W501 dead trigger: event 'WorkItem' … has no producer`. That's
> expected here: nothing *emits* `WorkItem`, you trigger it by hand. In a real system a triage
> workflow or a sensor would emit it, and the warning goes away. The compile still exits 0.
