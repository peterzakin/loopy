# effective-agents — the Anthropic agent patterns, as Loopy workflows

Anthropic's [**Building Effective Agents**](https://www.anthropic.com/research/building-effective-agents)
post — and its [cookbook companion](https://github.com/anthropics/anthropic-cookbook/tree/main/patterns/agents)
— catalogs five composable workflow patterns. The cookbook shows each as Python that chains
LLM calls. This project shows each as a **Loopy workflow**: the same composition, but authored
as files and driven by the engine, so it versions, diffs, and reviews like the rest of a repo.

One project, five workflows — `loopy compile examples/effective-agents` draws all five DAGs:

| Pattern (cookbook) | Workflow | Shape | Loopy mechanism |
|---|---|---|---|
| **Prompt chaining** | `chaining/` | extract → format → sort | a linear `after:` chain |
| **Routing** | `routing/` | classify → respond | route is a typed **output**; the branch is in agent prose |
| **Parallelization** | `parallel/` | intake → (security ‖ performance ‖ style) → aggregate | fan-out + `after: [a, b, c]` join |
| **Orchestrator-workers** | `orchestrator/` | plan → work → synthesize | plan is an output; the worker fans out *inside* one step |
| **Evaluator-optimizer** | `evaluator/` | generate → evaluate ↺ | a **loop-back event** (`emits:` the workflow's own `on:` event) |

## The one thing that's different from the cookbook

The cookbook drives control flow in Python — `if route == "billing": ...`, `while not
good_enough: ...`, `for task in subtasks: spawn(...)`. Loopy's DAG edges are **static**:
`after:` is fixed at compile time and there are no conditional edges. So those decisions move
to where Loopy keeps control flow — **in the agent** (prose + skills) and **on the bus**
(events). Two consequences worth seeing in the files:

- **Branches become data + prose.** `routing/classify` emits a typed `route`; `routing/respond`
  carries every playbook and follows the one the route names. The decision is real and
  inspectable (it's an output), but it isn't a graph edge.
- **Loops become events.** `evaluator/evaluate` `emits: ContentRequested` — the very event its
  own workflow triggers `on:` — re-entering at `generate` with feedback. It emits only on
  `fail`, so the loop's stop condition lives in the step's prose. The compiler draws this as
  `evaluator ─▶ ContentRequested ─▶ evaluator`.

Where the cookbook *does* map straight across, it does: prompt chaining is a plain `after:`
chain, and parallelization's fixed sections are real parallel steps joined by `after: [...]`.
Orchestrator-workers is the in-between case — see the note in `orchestrator/work.md` for why a
runtime-decided worker count collapses into one worker step here.

## Run one

These workflows are text-in/text-out, so the sandbox only needs the model API. Provide a key
and fire any entry event by hand (from the repo root; drop `uv run` if `loopy` is installed):

```bash
mkdir -p examples/effective-agents/secrets
echo 'ANTHROPIC_API_KEY=sk-ant-...' > examples/effective-agents/secrets/dev.env

uv run loopy compile examples/effective-agents

# prompt chaining
uv run loopy trigger manifest.json --root examples/effective-agents \
  --event TextSubmitted \
  --fields '{"text": "Revenue grew to $4.2M, churn fell to 3%, NPS rose to 61."}'

# routing
uv run loopy trigger manifest.json --root examples/effective-agents \
  --event SupportQuery \
  --fields '{"query": "I was double-charged this month", "customer": "acme-co"}'

# evaluator-optimizer (loops until the Judge passes — keep a round cap in the brief)
uv run loopy trigger manifest.json --root examples/effective-agents \
  --event ContentRequested \
  --fields '{"brief": "A 2-sentence release note for dark mode. Stop after 3 rounds.", "feedback": ""}'
```

`loopy trigger` warns `LOOPY-W501 dead trigger` for these events — expected, since no sensor
produces them; you're firing them by hand. See the top-level [`README.md`](../../README.md)
for the authoring model and [`examples/codefix/`](../codefix/) for the full "run locally" setup
(git auth, sandbox providers, the dashboard).
