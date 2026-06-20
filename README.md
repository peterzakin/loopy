# Loopy

Loopy is a framework for authoring agent automations**. The agent's** job is the **body** (prose); config is light **frontmatter**; the workflow's DAG is built from one rule.

## Install

Loopy isn't published to PyPI yet, so install it from a checkout of this repo:

```bash
uv tool install .            # from the repo root: puts `loopy` on your PATH
loopy init my-project        # scaffold a project, then `cd my-project`
```

Every `loopy` command below assumes it's on your PATH. Prefer not to install? Prefix each
command with `uv run` from the repo (e.g. `uv run loopy compile`).

**Per-project convention.** A project is a directory, and its credentials live inside it:
`secrets/dev.env` (the sandbox's environment — `ANTHROPIC_API_KEY`) and `loopy.env`
(control-plane creds, written by `loopy auth github`). Both are gitignored. Run every command
from the project directory so `loopy.env` and `--root` stay in sync — `loopy init` sets this up
for you.

## Workflows

A **workflow** is a directory. Inside it:

- Exactly **one entry step** carries `on:` — a single **registered** Event, or a **built-in time
trigger** `on: cron("<expr>")`. A step triggers on **exactly one** event; fan-in from many sources is
done at the **sensor layer** (several sensors emit one normalized event — e.g. `Incident`).
- **Every other step** carries `after: <step>` (or `after: [a, b]`), consuming the
predecessor's **outputs**.
- Data flows by reference: `{{ event.field }}` (from the triggering event) or `{{ step.field }}`
(an **output** of a step you're `after`). There is no `ref()`.
- Any step may also `emits:` a **registered** event for other workflows to subscribe to (see
*Outputs and events* below).

The engine builds the DAG directly: the `on:` step is the root, `after:` edges are the
order. A step with neither `on:` nor `after:` is an orphan — a compile-time error.

`cron("<expr>")` takes a **quoted** 5-field cron expression (optional `, tz=...`) — the quotes
keep commas in the expression (`cron("1,15 * * * *")`) from colliding with the `tz=` separator. It
needs no registry entry — it's built in. The step receives a tick as its event, with `{{ event.scheduled_at }}`
and `{{ event.last_run }}` so it can scan only what changed since it last ran.

## Layout

```
ProjectName/
  registry.yml                  # reused, Capitalized entities: Agents · Sandboxes · Events
  workflows/                    # each subdirectory is one single-entry workflow
    triage/      investigate.md                          # on: Incident → WorkItem
    upkeep/      scan-deps.md                            # on: cron("0 3 * * *") → WorkItem
    resolve/     arbitrate.md · fix.md · review.md · ship.md
    confirm/     check.md                                # on: MetricThreshold
  skills/                       # reusable agent skills, referenced by name from registry.yml
    triage/             SKILL.md
    repro-authoring/    SKILL.md
    rubrics/fix-quality/ SKILL.md                        # namespaced
  sensors/                      # the event-publish layer — code that emits registered events
    sensors.py
```

The incidents loop is **four workflows** wired by events only at the real seams — `Incident`
(sensors → triage), `WorkItem` (triage → resolve, and `upkeep`'s nightly `cron` → resolve),
`MetricThreshold` (sensor → confirm), and `GoalShipped` (resolve's terminal announcement) — while
the tight internals (`arbitrate → fix → review → ship`) pass **outputs** along `after`-chains.

## Naming convention

Defined entities are **Capitalized types** — `WorkItem`, `Investigator`, `MetricThreshold` — and
references point at them by that name (`on: WorkItem`, `agent: Investigator`). Filenames, step
names, and event *fields* (`event.issue_id`) stay lowercase. (The built-in `default` sandbox is
the one reserved lowercase name.)

## A file

```
---
after:  fix                 # or `on: <RegisteredEvent>` for the one entry step
agent:  Reviewer            # from registry.yml
output: { verdict: enum[pass, fail], notes: str }   # structured outputs, typed
# emits: <RegisteredEvent>  # optional — only if another workflow subscribes to it
budget: { wall_clock: 20, spend: { usd: 4 } }   # wall_clock in minutes; window/latency in days
---
the agent's objective, in prose — reads {{ event.* }} and {{ fix.diff }} (an output of `fix`)
```

## Outputs and events

A step can produce two kinds of result, and they're different things:

- **Outputs** are a step's structured data results, declared on the step (`output:`, a typed
field map). A downstream step in the **same** workflow consumes them with `after:` +
`{{ step.field }}`. Outputs are **not** events — they never touch the bus.
- **Events** are emitted (`emits:`) onto the shared bus for **other** workflows to subscribe to
(`on:`). Events must be formally **registered** in `registry.yml`; the bus only routes
registered events, and an `on:` trigger requires its event to exist in the registry. Sensors
publish events too — `on:` doesn't care whether a sensor or a step emitted it.

The test for which to reach for: *does another workflow need this value?* If the next step in
the same workflow consumes it, it's an **output**. If another workflow subscribes to it — or a
step needs to loop back to a workflow's entry — it's an **event**. Within-workflow handoffs
(e.g. `arbitrate → fix`) are outputs; cross-workflow seams (`investigate → arbitrate` via
`WorkItem`) are events.

## Example `registry.yml`

The reused entities, defined once and referenced by name. (Condensed — the full file defines
more agents and events.)

```yaml
# Defaults — every agent inherits these; override a field only when needed.
# `harness.runtime` picks the agent runner: `claude-code` (Claude Code, claude-* models) or
# `codex` (OpenAI Codex, gpt-*/o-series/codex-* models). Model must match the runtime.
defaults:
  agent:
    sandbox: default
    harness: { runtime: claude-code, model: claude-sonnet-4-6 }

# Sandbox — compute + egress. `image:` is the declarative build; `network:` the egress allowlist.
# `env_file:` points at a gitignored dotenv whose keys are injected as the sandbox's environment
# (the sandbox inherits *nothing* from your shell — secrets like `ANTHROPIC_API_KEY` must live
# here). `repos:` are cloned into the workspace at acquire time, with git auth injected (see
# "Examples / run it locally").
sandboxes:
  default:
    provider: daytona
    image: { debian_slim: "3.12", apt: [git], workdir: /home/daytona, user: daytona }
    network: [github.com]
    env_file: secrets/dev.env        # gitignored; injected as the sandbox's env
    repos: [octocat/Hello-World]     # cloned into the workspace at acquire time (git auth injected)

# Agents — capability comes from the sandbox (image + egress), skills, injected creds, and
# budget; numeric caps live in budget, not in a tool name.
agents:
  Investigator: { skills: [triage, repro-authoring] }                  # inherits default harness
  Fixer:        { harness: { model: claude-opus-4-8 }, skills: [testing] }
  Reviewer:     { skills: [rubrics/fix-quality] }                       # a judge — review-only skill
  Releaser:     { skills: [rollout] }
  Scout:        { harness: { runtime: codex, model: gpt-5 }, skills: [triage] }   # runs on OpenAI Codex

# Events — the bus contract. A step's `on:` may only name an event registered here.
# Typed field maps.
events:
  # published by sensors
  Incident:        { fields: { source: enum[sentry, linear, datadog, pagerduty, slack], issue_id: id, title: str, link: url } }
  MetricThreshold: { fields: { goal_id: id } }
  # emitted by steps — cross-workflow seams + terminal announcements
  WorkItem:
    fields:
      source:        enum[sentry, linear, datadog, pagerduty, slack, cve]   # Incident's 5 sources, carried through; + cve via upkeep's cron
      link:          url
      root_cause:    str
      proposed_goal: str
      repro:         str
  GoalShipped:     { fields: { goal_id: id } }                         # terminal announcement
```

## `skills/`

Reusable agent skills, a sibling of `workflows/`. One directory per skill — a `SKILL.md` plus
any resources — and agents reference them **by name** in `registry.yml`
(`skills: [triage, rubrics/fix-quality]`). Define a skill once, reuse it across agents.
Namespaced subdirectories are allowed (`rubrics/fix-quality`). A skill name resolves only against
`skills/`; an unresolved name is a compile-time error.

This reflects the organizing principle: `**registry.yml` holds the lightweight, inline config**  
**entities** (Agents, Sandboxes, Events — a few fields each), while **top-level directories hold**  
**authored artifacts that have a *body*** (`workflows/`, `skills/`, and eventually `sensors/`). An  
agent naming `skills: [triage]` resolves it against `skills/`, the same way `agent: Investigator`  
resolves against the registry.

## `sensors/`

The event-publish layer: code that turns the outside world into **registered events**. One or
more files (a single `sensors.py` is fine); each sensor is a function decorated with `@sensor`,
triggered by a `poll` or a `webhook`.

> **Note:** `poll` is the intended near-term trigger. **`webhook` is a future improvement** —
> hosting webhooks for arbitrary third-party services needs per-source authentication, which is
> deferred. The `webhook=` examples below illustrate that future path.

A sensor **returns a registered event** — and returning *is* emitting: the event goes on the
bus and routes to whichever workflow subscribes with `on:`. Return `None` to emit nothing, or
`yield` an `Iterator[Event]` to emit several.

**Compile rule.** A sensor must **declare** the event it emits via `emits=` (a registered event
from `registry.yml`), in a form `loopy compile` can read statically — it never imports or runs
your code. A sensor that declares no `emits`, names an unregistered event, or builds its
declaration imperatively (so it can't be read statically) fails to load — `loopy compile` errors
before anything runs. That's what guarantees every event on the bus has a contract. The return
type (`-> Incident`) is optional: it's checked by *your* typechecker (mypy) against `loopy.events`,
not by the compiler.

```python
from loopy import sensor
from loopy.events import Incident             # generated from registry.yml — optional, for your typechecker

@sensor(webhook="/hooks/sentry", emits="Incident")   # `emits` is the contract the compiler reads
def sentry_issues(req) -> Incident:                  # return type optional; mypy checks the payload shape
    i = req.json["data"]["issue"]
    return Incident(source="sentry", issue_id=i["id"], title=i["title"], link=i["permalink"])
```

Sensors can be written in other languages too. Without free-function decorators (e.g. TypeScript),
declare them in a single statically-analyzable `sensorRegistry` literal instead — same contract, a
declared `emits` next to the trigger:

```typescript
import type { Incident } from "loopy/events";        // generated — optional, for tsc

export const sensorRegistry = {
  sentryIssues: {
    webhook: "/hooks/sentry",
    emits: "Incident",                                // the contract the compiler reads
    handler: (req): Incident => ({
      source: "sentry", issue_id: req.body.data.issue.id,
      title: req.body.data.issue.title, link: req.body.data.issue.permalink,
    }),
  },
};
```

See `sensors/sensors.py` for the full example.

## Examples / run it locally

- [`examples/incidents/`](examples/incidents/) — the canonical multi-workflow loop this README
  describes (triage → resolve → confirm, plus an `upkeep` cron scan).
- [`examples/codefix/`](examples/codefix/) — the smallest *runnable* loop: one `CodeTask` event →
  an agent that edits a checkout and opens a PR. Start here to actually run something. Its README
  is a **"Run locally" quickstart** — what each `--sandbox` needs in its `env_file`
  (`ANTHROPIC_API_KEY`/`GITHUB_TOKEN`, plus `PATH`/`HOME` for bare `local`), how to wire git auth
  with `loopy auth github`, and a one-command end-to-end smoke test. Tokens are injected only when
  a GitHub App is configured (on both `run` and `trigger`); they are **not** ambient on the
  `trigger`/`local` path — the quickstart spells out the difference.

A few things worth knowing before the first run:

- **`loopy doctor` checks a project is *runnable*, not just valid.** A green `loopy compile` only
  proves the manifest is well-formed; the scaffold still ships placeholders that break a real run (a
  fake `ANTHROPIC_API_KEY`, an unpushable starter repo, no git auth). `loopy doctor` names exactly
  which of those are still outstanding — run it before your first `trigger`.
- **`--sandbox` is a provider, not a sandbox name.** It takes `local | docker | daytona` and
  selects the *provider* for the run; it is **not** the name of a sandbox in `registry.yml`. (Per-
  sandbox provider selection from the registry isn't wired yet — the flag picks one provider for
  the whole run.)
- **The sandbox inherits nothing from your shell.** Everything an agent needs — the model key,
  any git token — must be in the sandbox's `env_file`; exporting `ANTHROPIC_API_KEY` in your shell
  is not enough. (The bare `local` provider also needs `PATH`/`HOME` there; `docker`/`daytona` get
  those from the image.)
- **`loopy compile <path>` generates a `loopy/` events package** under the project (`loopy/events.py`
  + stubs, for your typechecker). It's already gitignored; pass `--out manifest.json` to also write
  the manifest.
- **A hand-fired event warns `LOOPY-W501 dead trigger`.** When you drive a workflow with
  `loopy trigger --event X` and no sensor produces `X`, compile flags it as a dead trigger. That's
  **expected** for the manual-trigger pattern — it's a warning, not an error, and the run proceeds.

## Watching runs

The dev server `loopy run --in-process` records every run to a durable on-disk store
(`.loopy/state.db` by default), and `loopy admin` serves a small read-only dashboard over it — a
run list with each run's step timeline, emitted events, outputs, and any failure:

```bash
loopy run --in-process manifest.json   # dev server: records runs as they execute
loopy admin                            # in another terminal → http://127.0.0.1:9000
```

`loopy admin` reads the same DB the dev server writes, so it needs no flags. (A bare `loopy run`
brings up the containerized stack instead, which keeps its state in a Docker volume; the one-shot
`loopy trigger` path is in-memory and isn't recorded.) See [`DEPLOYMENT.md`](DEPLOYMENT.md) for the
`state:` config block and caveats.

