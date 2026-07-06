# Loopy cookbook

Each subdirectory here is a **self-contained Loopy project** — its own `registry.yml`,
`workflows/`, and (where it helps) `sensors/` and `skills/` — with a README that explains what
it shows and how to run it. They're meant to be read in `loopy compile` order: compile one,
look at the DAG it prints, then open the files it points to.

The examples are grouped by what you're trying to learn. The loops showcased at
[loopy.computer/examples](https://loopy.computer/examples.html) live here too — each of
those pages is a step-by-step walkthrough of its matching directory (`dep-upkeep/`,
`github/`, `issue-triage/`, `changelog/`, `uptime/`, `customer-feedback-loop/`).

## Start here — run something

| Example | What it shows |
|---|---|
| [`codefix/`](codefix/) | The smallest *runnable* loop: one `CodeTask` event → an agent edits a checkout and opens a PR. Its README is the canonical **"run locally"** quickstart (sandbox providers, git auth, the dashboard). Start here to actually run something. |

## Scheduled loops — time-driven, zero sensor code

| Example | What it shows |
|---|---|
| [`dep-upkeep/`](dep-upkeep/) | The **cron** loop, in the spirit of Dependabot: every night, one agent finds outdated or vulnerable dependencies and opens a single PR that bumps them. `on: cron("0 3 * * *")` is built in; the tick's `scheduled_at` / `last_run` fields do the bookkeeping. |

## GitHub built-ins — triggered on `Github.*`, zero sensor code

| Example | What it shows |
|---|---|
| [`github/`](github/) | The **built-in event** loop. A workflow triggers on `Github.PullRequestOpened` / `Github.PullRequestMerged` with no sensor or event declaration; the compiler injects the contract + a `/hooks/github` sensor, the signature is verified at the edge, and the matching workflow fires (PR opened → review, PR merged → find follow-on work). |
| [`issue-triage/`](issue-triage/) | The issue-side companion: on `Github.IssueOpened`, an agent classifies the issue into a **typed** area and severity (`enum[...]` outputs, validated, not free text) and posts a triage comment. |
| [`changelog/`](changelog/) | On `Github.PullRequestMerged`, an agent drafts the changelog entry the merge implies and opens a small PR adding it, so the changelog never falls behind the code. |

## Event-driven loops — the canonical shapes

| Example | What it shows |
|---|---|
| [`incidents/`](incidents/) | The **multi-workflow** loop from the top-level README: triage → resolve → confirm, plus a nightly `upkeep` cron scan — four workflows wired by events only at the real seams. |
| [`uptime/`](uptime/) | The smallest look at the **sensor layer**: a poll sensor checks a health endpoint and emits an `Incident` only when it is down (returning `None` emits nothing); a second workflow reacts by opening a GitHub issue. |
| [`customer-feedback-loop/`](customer-feedback-loop/) | The **webhook sensor** loop: each new Zendesk ticket becomes a typed `CustomerTicket`, and one agent decides whether it points at real work — opening a PR that links back to the ticket when it does, and doing nothing when it doesn't. |

## Patterns from the Anthropic cookbook

| Example | What it shows |
|---|---|
| [`effective-agents/`](effective-agents/) | Anthropic's [**Building Effective Agents**](https://github.com/anthropics/anthropic-cookbook/tree/main/patterns/agents) patterns — prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer — each re-authored as a Loopy workflow. A good map of how the cookbook's Python control flow (`if`/`while`/`for`) becomes static DAG edges, typed outputs, and loop-back events. |

## Research loops

| Example | What it shows |
|---|---|
| [`auto-research/`](auto-research/) | A self-driving research loop in the spirit of Karpathy's "automated research": poll papers → digest → hypothesize → run a small experiment → write up → reflect and loop. Shows a bounded self-perpetuating loop (depth guard + budgets) built only from existing primitives. |

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
