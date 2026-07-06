# Loopy cookbook

Each subdirectory here is a **self-contained Loopy project** — its own `registry.yml`,
`workflows/`, and (where it helps) `sensors/` and `skills/` — with a README that explains what
it shows and how to run it. They're meant to be read in `loopy compile` order: compile one,
look at the DAG it prints, then open the files it points to.

The examples are grouped by what you're trying to learn.

## Start here — run something

| Example | What it shows |
|---|---|
| [`codefix/`](codefix/) | The smallest *runnable* loop: one `CodeTask` event → an agent edits a checkout and opens a PR. Its README is the canonical **"run locally"** quickstart (sandbox providers, git auth, the dashboard). Start here to actually run something. |

## Event-driven loops — the canonical shapes

| Example | What it shows |
|---|---|
| [`github/`](github/) | The **built-in event** loop. A workflow triggers on `Github.PullRequestOpened` / `Github.PullRequestMerged` with no sensor or event declaration; the compiler injects the contract + a `/hooks/github` sensor, the signature is verified at the edge, and the matching workflow fires (PR opened → review, PR merged → find follow-on work). |
| [`incidents/`](incidents/) | The **multi-workflow** loop from the top-level README: triage → resolve → confirm, plus a nightly `upkeep` cron scan — four workflows wired by events only at the real seams. |

## Patterns from the Anthropic cookbook

| Example | What it shows |
|---|---|
| [`effective-agents/`](effective-agents/) | Anthropic's [**Building Effective Agents**](https://github.com/anthropics/anthropic-cookbook/tree/main/patterns/agents) patterns — prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer — each re-authored as a Loopy workflow. A good map of how the cookbook's Python control flow (`if`/`while`/`for`) becomes static DAG edges, typed outputs, and loop-back events. |

## Conventions every example follows

- **One directory, one project.** A subdirectory is everything `loopy compile <dir>` needs.
- **`registry.yml` holds the inline config** (Agents · Sandboxes · Events); the directories
  with bodies (`workflows/`, `skills/`, `sensors/`) hold the authored artifacts. This mirrors
  the organizing principle in the top-level [`README.md`](../README.md).
- **Secrets are gitignored.** Each example ships a `base.env.example`; copy it to
  `secrets/base.env` (the path the registry's `env_file:` references) and fill it in.
- **Compile first.** `uv run loopy compile examples/<name>` is the gate — it prints the DAG and
  fails if any `on:`/`emits:` names an unregistered event or any `{{ }}` ref doesn't resolve.

New to Loopy? Read the top-level [`README.md`](../README.md) for the authoring model first,
then `codefix/` to run one end-to-end.
