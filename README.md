# Loopy

**Open source (Apache-2.0) · Python 3.12+**

Loopy is an *open-source*, *agent-neutral* framework for authoring agent automations. Your
automations are *files in your repo* (workflows, skills, and sensors authored as Markdown and
code), so they version, diff, and review like the rest of your codebase. There's no canvas to
click together: `loopy compile` builds the workflow's DAG straight from those files.

**Agent-neutral.** Loopy orchestrates the loop; it doesn't bind you to one vendor's agent. Every
agent in `registry.yml` names its `harness` — the runner that drives it: **Claude Code**,
**OpenAI Codex**, and **OpenCode** ship today, and the harness registry is built to
take more. Mix them in one manifest: route a triage step to one harness and a fixer to another,
and swap a step's harness or model without touching its prose.

## Install

Loopy is published to PyPI as [`loopy-computer`](https://pypi.org/project/loopy-computer/):

```bash
uv tool install loopy-computer   # puts `loopy` on your PATH
loopy init my-project            # scaffold a project, then `cd my-project`
```

Working from a checkout of this repo instead? `uv tool install .` from the repo root does the
same thing with your local source.

Every `loopy` command below assumes it's on your PATH. Prefer not to install? Prefix each
command with `uv run` from the repo (e.g. `uv run loopy compile`).

**Per-project convention.** A project is a directory, and its credentials live inside it:
`secrets/base.env` (sandbox env), `loopy.env`
(control-plane creds: keys like `DAYTONA_API_KEY`, plus the GitHub App entries that
`loopy auth github` writes). All are gitignored. Run every command from the project directory so
`loopy.env` and `--root` stay in sync; `loopy init` sets this up for you. `init` is
interactive: it asks for the public base URL webhooks are delivered at (stored as
`LOOPY_PUBLIC_URL`; each webhook sensor receives deliveries at that base plus its path, e.g.
`<base>/hooks/github`), asks which repo(s) the agent should work on, offers to fill in
credentials it finds in your environment (`ANTHROPIC_API_KEY`, `DAYTONA_API_KEY`), offers to
register GitHub webhooks when the URL, App, and repos are all in place (`loopy webhooks
github` does the same any time later), and finishes by running the same checks as
`loopy doctor` so you know what's left before a first run.

## Using Loopy with an AI agent

Most first drafts of a loop are written by a coding agent ("build me a loop that…"), so the
toolchain is built to be driven headlessly:

- Every scaffolded project ships an **`AGENTS.md`** — the one-page map Claude Code, Codex,
  and OpenCode auto-discover: authoring rules, the verify loop, the secrets model.
- **`loopy docs`** prints the full authoring reference as markdown straight from the CLI
  (version-matched to the install, works offline); `loopy docs errors` prints the stable
  `LOOPY-E` diagnostic catalog.
- The verify loop is exit-code-clean: `loopy compile --check` (valid?), `loopy doctor`
  (runnable?), and `loopy trigger --json` (one run end-to-end, full record on stdout).
- The two commands that need a human are `loopy init` (an interactive setup wizard — it
  refuses to run without a terminal) and `loopy auth github` (a browser flow). For git
  auth the headless alternative is a `GITHUB_TOKEN` (`contents:write` +
  `pull_requests:write`) in the sandbox's `env_file`.

## Workflows

A **workflow** is a directory. Inside it:

- Exactly **one entry step** carries `on:`, either a single **registered** Event or a **built-in
time trigger** `on: cron("<expr>")`. A step triggers on **exactly one** event; fan-in from many sources is
done at the **sensor layer** (several sensors emit one normalized event, e.g. `Incident`).
- **Every other step** carries `after: <step>` (or `after: [a, b]`), consuming the
predecessor's **outputs**.
- Data flows by reference: `{{ event.field }}` (from the triggering event) or `{{ step.field }}`
(an **output** of a step you're `after`). There is no `ref()`.
- Any step may also `emits:` a **registered** event for other workflows to subscribe to (see
*Outputs and events* below).

The engine builds the DAG directly: the `on:` step is the root, `after:` edges are the
order. A step with neither `on:` nor `after:` is an orphan, a compile-time error.

`cron("<expr>")` takes a **quoted** 5-field cron expression (optional `, tz=...`); the quotes
keep commas in the expression (`cron("1,15 * * * *")`) from colliding with the `tz=` separator. It's
built in, so it needs no registry entry. The step receives a tick as its event, with `{{ event.scheduled_at }}`
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
  sensors/                      # the event-publish layer: code that emits registered events
    sensors.py
```

The incidents loop is **four workflows** wired by events only at the real seams: `Incident`
(sensors → triage), `WorkItem` (triage → resolve, and `upkeep`'s nightly `cron` → resolve),
`MetricThreshold` (sensor → confirm), and `GoalShipped` (resolve's terminal announcement). The
tight internals (`arbitrate → fix → review → ship`) pass **outputs** along `after`-chains.

## Naming convention

Defined entities are **Capitalized types** (`WorkItem`, `Investigator`, `MetricThreshold`), and
references point at them by that name (`on: WorkItem`, `agent: Investigator`). Filenames, step
names, and event *fields* (`event.issue_id`) stay lowercase.

## A file

```
---
after:  fix                 # or `on: <RegisteredEvent>` for the one entry step
agent:  Reviewer            # from registry.yml
output: { verdict: enum[pass, fail], notes: str }   # structured outputs, typed
# emits: <RegisteredEvent>  # optional: only if another workflow subscribes to it
budget: { wall_clock: 20, spend: { usd: 4 } }   # wall_clock in minutes; window/latency in days
---
the agent's objective, in prose; reads {{ event.* }} and {{ fix.diff }} (an output of `fix`)
```

## Outputs and events

A step can produce two kinds of result, and they're different things:

- **Outputs** are a step's structured data results, declared on the step (`output:`, a typed
field map). A downstream step in the **same** workflow consumes them with `after:` +
`{{ step.field }}`. Outputs are **not** events; they never touch the bus.
- **Events** are emitted (`emits:`) onto the shared bus for **other** workflows to subscribe to
(`on:`). Events must be formally **registered** in `registry.yml`; the bus only routes
registered events, and an `on:` trigger requires its event to exist in the registry. Sensors
publish events too; `on:` doesn't care whether a sensor or a step emitted it.

The test for which to reach for: *does another workflow need this value?* If the next step in
the same workflow consumes it, it's an **output**. If another workflow subscribes to it (or a
step needs to loop back to a workflow's entry), it's an **event**. Within-workflow handoffs
(e.g. `arbitrate → fix`) are outputs; cross-workflow seams (`investigate → arbitrate` via
`WorkItem`) are events.

## Example `registry.yml`

The reused entities, defined once and referenced by name. (Condensed; the full file defines
more agents and events.)

```yaml
# Defaults: every agent inherits these; override a field only when needed.
# `model` and `harness` are both required on every agent (here they come from defaults).
# `harness` picks the agent runner: `claude-code` (Claude Code, claude-* models),
# `codex` (OpenAI Codex, gpt-*/codex-* models), or `opencode` (OpenCode, which
# drives either family — write the bare model id; loopy expands it to opencode's
# provider/model naming). The model must be one its harness can drive; nothing is
# inferred, and `loopy compile` rejects a cross-provider pairing (a gpt-* model on
# claude-code, or vice versa) as a compile-time error (LOOPY-E508).
defaults:
  agent:
    sandbox: default
    model: claude-sonnet-4-6
    harness: claude-code

# Sandbox: compute + egress. `image:` is the declarative build; `network:` an optional egress
# allowlist (omit it for open egress; set it to restrict the sandbox to named hosts).
# `env_file:` points at a gitignored dotenv (a path, or a list of paths merged in order) whose
# keys are injected as the sandbox's environment
# (the sandbox inherits *nothing* from your shell; secrets like `ANTHROPIC_API_KEY` must live
# here). `repos:` are cloned into the workspace at acquire time, with git auth injected (see
# "Examples / run it locally").
sandboxes:
  default:
    provider: daytona
    image: { debian_slim: "3.12", apt: [git], workdir: /home/loopy, user: loopy }
    network: [github.com, api.anthropic.com]   # optional: restrict egress to git over https + the model API
    env_file: secrets/base.env        # gitignored; injected as the sandbox's env
    repos: [octocat/Hello-World]     # cloned into the workspace at acquire time (git auth injected)

# Agents: capability comes from the sandbox (image + egress), skills, injected creds, and
# budget; numeric caps live in budget, not in a tool name.
agents:
  Investigator: { skills: [triage, repro-authoring] }                  # inherits default model + harness
  Fixer:        { model: claude-opus-4-8, skills: [testing] }           # bigger model, same harness
  Reviewer:     { skills: [rubrics/fix-quality] }                       # a judge, review-only skill
  Releaser:     { skills: [rollout] }
  Scout:        { model: gpt-5, harness: codex, skills: [triage] }      # runs on OpenAI Codex
  Sweeper:      { model: claude-sonnet-4-6, harness: opencode, skills: [triage] }  # runs on OpenCode

# Events: the bus contract. A step's `on:` may only name an event registered here.
# Typed field maps.
events:
  # published by sensors
  Incident:        { source: enum[sentry, linear, datadog, pagerduty, slack], issue_id: str, title: str, link: url }
  MetricThreshold: { goal_id: str }
  # emitted by steps: cross-workflow seams + terminal announcements
  WorkItem:
    source:        enum[sentry, linear, datadog, pagerduty, slack, cve]   # Incident's 5 sources, carried through; + cve via upkeep's cron
    link:          url
    root_cause:    str
    proposed_goal: str
    repro:         str
  GoalShipped:     { goal_id: str }                                    # terminal announcement
```

> **No built-in agents.** Every agent a step names must be declared in `registry.yml` — the
> model + harness pairing is always explicit and visible, never injected or inferred.
> `loopy init` scaffolds one agent per supported harness (`claude-code`, `codex`,
> `opencode`) so the yaml for each is there to point a step at or edit.

Beyond a single step's `budget:`, the registry takes a top-level `limits:` block for wider spend
caps: `cascade_spend: { usd: <n> }` caps the total spend of an entire event cascade, and
`workflows: { <Name>: { spend: { usd: <n> } } }` caps one named workflow.

### Event field types

An event's fields are a `name: type` map. Loopy has no type system of its own: a field type is
just a **JSON Schema** fragment (draft 2020-12), validated at runtime by pydantic and code-generated
into the `loopy.events` classes. The terse forms below are authoring sugar over that.

| Authored | Meaning | JSON Schema emitted |
|---|---|---|
| `str` | string | `{"type": "string"}` |
| `int` | integer | `{"type": "integer"}` |
| `float` | number | `{"type": "number"}` |
| `bool` | boolean | `{"type": "boolean"}` |
| `url` | string, URI-formatted | `{"type": "string", "format": "uri"}` |
| `enum[a, b, c]` | one of a fixed set of strings | `{"type": "string", "enum": ["a", "b", "c"]}` |

The same field types apply to a step's `output:` map. An unknown bare shorthand (not in the table
above and not a valid schema object) is a compile-time error (`LOOPY-E201`).

## `skills/`

Reusable agent skills, a sibling of `workflows/`. One directory per skill (a `SKILL.md` plus
any resources), and agents reference them **by name** in `registry.yml`
(`skills: [triage, rubrics/fix-quality]`). Define a skill once, reuse it across agents.
Namespaced subdirectories are allowed (`rubrics/fix-quality`). A skill name resolves only against
`skills/`; an unresolved name is a compile-time error.

This reflects the organizing principle: **`registry.yml` holds the lightweight, inline config
entities** (Agents, Sandboxes, Events; a few fields each), while **top-level directories hold the
authored artifacts that have a *body*** (`workflows/`, `skills/`, and `sensors/`). An
agent naming `skills: [triage]` resolves it against `skills/`, the same way `agent: Investigator`
resolves against the registry.

## `sensors/`

The event-publish layer: code that turns the outside world into **registered events**. One or
more files (a single `sensors.py` is fine); each sensor is a function decorated with `@sensor`,
triggered by a `poll` or a `webhook`.

> **Common GitHub and Sentry events are built in; you may not need a sensor at all.** A
> workflow can trigger on a platform-shipped event directly (`on: Github.PullRequestOpened`,
> `on: Sentry.IssueCreated`) with no `registry.yml` entry and no `sensors/` file; the compiler
> injects the contract and a `/hooks/<provider>` sensor for you. Catalog:
> `Github.PullRequestOpened`, `PullRequestMerged`, `IssueOpened`, `IssueCommentCreated`,
> `Push`; `Sentry.IssueCreated`, `IssueResolved`, `AlertTriggered`. The `Github.` and
> `Sentry.` namespaces are reserved. See [`examples/github/`](examples/github/). Write your
> own `@sensor` (below) for any source the built-ins don't cover.

> **Both `poll` and `webhook` are supported.** `loopy run` hosts each `@sensor(webhook=...)` as an
> HTTP route and fans one path out to every sensor on it (GitHub posts every event type to a single
> URL, so several sensors can share `/hooks/github`). A sensor's public delivery URL is one base
> plus its path: set `LOOPY_PUBLIC_URL` in `loopy.env` (prompted for at `loopy init`) to your
> deployed host or dev tunnel; `loopy webhooks list` prints each full delivery URL
> (e.g. `<base>/hooks/github`), and **`loopy webhooks github`** registers GitHub's side for you —
> it creates a webhook on each repo in `registry.yml` via the App from `loopy auth github` and
> lands the signing secret in `loopy.env` (`--check` reports without changing anything; built-in
> `Github.*` triggers never fire until this or a manual registration exists). When
> `GITHUB_WEBHOOK_SECRET` is set, `loopy run` verifies GitHub's `X-Hub-Signature-256` HMAC at the
> edge before any sensor sees the payload (likewise `SENTRY_WEBHOOK_SECRET` for Sentry's
> `Sentry-Hook-Signature` on `/hooks/sentry`). See
> [`examples/incidents/sensors/sensors.py`](examples/incidents/sensors/sensors.py)
> for a mix of `webhook` and `poll` sensors.

A sensor **returns a registered event**, and returning *is* emitting: the event goes on the
bus and routes to whichever workflow subscribes with `on:`. Return `None` to emit nothing, or
`yield` an `Iterator[Event]` to emit several.

**Compile rule.** A sensor must **declare** the event it emits via `emits=` (a registered event
from `registry.yml`), in a form `loopy compile` can read statically; it never imports or runs
your code. A sensor that declares no `emits`, names an unregistered event, or builds its
declaration imperatively (so it can't be read statically) fails to load: `loopy compile` errors
before anything runs. That's what guarantees every event on the bus has a contract. The return
type (`-> Incident`) is optional: it's checked by *your* typechecker (mypy) against `loopy.events`,
not by the compiler.

```python
from loopy import sensor
from loopy.events import Incident             # generated from registry.yml; optional, for your typechecker

@sensor(webhook="/hooks/sentry", emits="Incident")   # `emits` is the contract the compiler reads
def sentry_issues(req) -> Incident:                  # return type optional; mypy checks the payload shape
    i = req.json["data"]["issue"]
    return Incident(source="sentry", issue_id=i["id"], title=i["title"], link=i["permalink"])
```

Sensors are **Python-only today**: the compiler statically inspects every `.py` under `sensors/`
(subdirectories included; organize them however you like) and nothing else. The design generalizes
to other languages (a single statically-analyzable `sensorRegistry` literal for languages without
free-function decorators, e.g. TypeScript), but that surface isn't implemented yet.

**Poll intervals are plain durations.** `@sensor(poll="5m")` takes a whole number plus a unit
(`s`, `m`, `h`, or `d`: `"30s"`, `"1h"`, `"2d"`); a malformed interval is a compile-time error
(`LOOPY-E403`). Ticks never overlap (the next is scheduled after the current one finishes), and a
tick's watermark advances only after its events are delivered, so a failed poll re-covers the same
window and skips no data. The very first tick asks for exactly one interval of history. To run on
a clock rather than a gap, use `cron(...)` on a workflow's `on:` instead.

See [`examples/incidents/sensors/sensors.py`](examples/incidents/sensors/sensors.py) for a
`webhook` + `poll` example, and `sensors/sensors.py` in a scaffolded project. For common GitHub
events, prefer the built-in `Github.*` triggers (above); no sensor required.

## Examples / run it locally

[`examples/`](examples/) is the **cookbook**: each subdirectory is a self-contained project
with its own README, grouped in [`examples/README.md`](examples/README.md) (start-here,
event-driven loops, ports of the Anthropic cookbook, and research loops).

- [`examples/incidents/`](examples/incidents/): the canonical multi-workflow loop this README
  describes (triage → resolve → confirm, plus an `upkeep` cron scan).
- [`examples/effective-agents/`](examples/effective-agents/): Anthropic's *Building Effective
  Agents* patterns (prompt chaining, routing, parallelization, orchestrator-workers,
  evaluator-optimizer), each re-authored as a Loopy workflow.
- [`examples/auto-research/`](examples/auto-research/): a self-driving research loop in the
  spirit of Karpathy's "automated research": digest → hypothesize → experiment → write up →
  reflect, bounded by a depth guard and per-experiment budgets.
- [`examples/github/`](examples/github/): the canonical **webhook** loop. GitHub posts every
  event to one `/hooks/github` URL; `loopy run` verifies the `X-Hub-Signature-256` HMAC once at the
  edge, then fans the delivery out to two sensors (PR opened → code review, PR merged → find
  follow-on work).
- [`examples/codefix/`](examples/codefix/): the smallest *runnable* loop, one `CodeTask` event →
  an agent that edits a checkout and opens a PR. Start here to actually run something. Its README
  is a **"Run locally" quickstart**: what each sandbox `provider:` needs in its `env_file`
  (`ANTHROPIC_API_KEY`/`GITHUB_TOKEN`, plus `PATH`/`HOME` for bare `local`), how to wire git auth
  with `loopy auth github`, and a one-command end-to-end smoke test. Tokens are injected only when
  a GitHub App is configured (on both `run` and `trigger`); they are **not** ambient on the
  `trigger`/`local` path, and the quickstart spells out the difference.

A few things worth knowing before the first run:

- **`loopy doctor` checks a project is *runnable*, not just valid.** A green `loopy compile` only
  proves the manifest is well-formed; the scaffold still ships placeholders that break a real run (a
  fake `ANTHROPIC_API_KEY`, an unpushable starter repo, no git auth). `loopy doctor` names exactly
  which of those are still outstanding; run it before your first `trigger`.
- **Where an agent runs is authored in `registry.yml`, not on the command line.** Every sandbox
  must declare its `provider:` (`local | docker | daytona`); a sandbox without one is a
  compile-time error (E214), so where an agent runs is always explicit, never inferred. The
  runtime dispatches each step to the backend its sandbox names; there is no `--sandbox` flag, so
  two sandboxes in one manifest can target different backends. Every agent must itself name a
  sandbox, directly or via `defaults.agent.sandbox`, or it's a compile error (E506). `loopy init`
  scaffolds a `daytona` (remote) sandbox and points the default agent at it.
- **The sandbox inherits nothing from your shell.** Everything an agent needs (the model key,
  any git token) must be in the sandbox's `env_file`; exporting `ANTHROPIC_API_KEY` in your shell
  is not enough. (The bare `local` provider also needs `PATH`/`HOME` there; `docker`/`daytona` get
  those from the image.)
- **`loopy compile <path>` writes `manifest.json` by default** (`--out` changes the path;
  `--check` validates without writing, the CI gate). It also generates a `loopy/` events package
  under the project (`loopy/events.py` + stubs, for your typechecker), already gitignored. And
  there's no separate compile step in the dev loop: `loopy run` compiles a directory target and
  recompiles a stale manifest automatically.
- **A hand-fired event warns `LOOPY-W501 dead trigger`.** When you drive a workflow with
  `loopy trigger --event X` and no sensor produces `X`, compile flags it as a dead trigger. That's
  **expected** for the manual-trigger pattern: it's a warning, not an error, and the run proceeds.

## Watching runs

The dev server `loopy run --in-process` records every run to a durable on-disk store
(`.loopy/state.db` by default), and `loopy admin` serves a small read-only dashboard over it: a
run list with each run's step timeline, emitted events, outputs, and any failure. `loopy admin`
takes an optional deploy target to administer — it defaults to `local` (this dev loop); pass
`byo` or `bootstrap` for a hosted control plane (below):

```bash
loopy run --in-process manifest.json   # dev server: records runs as they execute
loopy admin                            # in another terminal → http://127.0.0.1:9000
```

`loopy admin` (target `local`) reads the same DB the dev server writes, so it needs no flags; with a
`manifest.json` present it also renders the workflow, sensor, and registry views, and `loopy demo`
serves every view against in-memory sample data. (A bare `loopy run` brings up the containerized
stack instead, a `redis` bus container plus the engine, which keeps its state in a Docker
volume; the one-shot `loopy trigger` path is in-memory and isn't recorded.) The deployment knobs
(`--bus inproc|redis`, `--state`, `--state-path`, `--host`/`--port`, `--detach`) are covered by
`loopy run --help`.

### Watching a hosted control plane

The loopback dashboard needs no auth. The moment it leaves the box it does: run and step outputs
are not redacted, so remote access is bearer-token-gated end to end. The engine serves the
dashboard itself, path-routed off the one public URL — webhook deliveries at
`$LOOPY_PUBLIC_URL/hooks/*`, the dashboard at `$LOOPY_PUBLIC_URL/admin` — so there's no second
service to deploy and the admin endpoint is deterministic on every provider:

```bash
# `loopy init` already minted LOOPY_ADMIN_TOKEN into loopy.env (the loopy_sk_ prefix makes
# leaks greppable). One value, used in two places:

# control plane: copy that value into LOOPY_ADMIN_TOKEN in the platform env. `loopy run` then
# mounts the dashboard at /admin behind it. Without the token, a non-loopback bind serves
# webhooks but no /admin at all (fail-closed by absence).

# laptop: the token (and LOOPY_PUBLIC_URL) are already in loopy.env, then:
loopy admin                       # proxies /api to $LOOPY_PUBLIC_URL/admin with the token
```

`loopy admin` routes itself off `LOOPY_PUBLIC_URL`: a normal URL is proxied at
`$LOOPY_PUBLIC_URL/admin`, a CloudFront URL (`*.cloudfront.net`, where `/admin` is blocked at
the edge) is reached over an SSM tunnel instead, and no URL at all reads the local run-state
DB. It runs a small local proxy: the token stays in that process (read from `loopy.env` or
the environment — never a query param, cookie, or browser variable), every request crosses
as `Authorization: Bearer` over TLS, and the server compares it in constant time. Override
the routing with `--url https://…` (proxy somewhere explicit), `--tunnel` (force the SSM
tunnel), or `--local` (force the local DB, e.g. a standalone `loopy admin --local --host
0.0.0.0` serving a copy of the state DB). Rotation is overlap-based: the server also accepts
`LOOPY_ADMIN_TOKEN_NEXT`, so you can roll both sides without a lockout. Plain HTTP to a
non-loopback remote is refused.

The serve contract is deliberately provider-agnostic — a process env var, `$PORT`, TLS
terminated at the platform ingress, and durable run state behind the `StateStore` protocol —
so nothing in loopy branches on the hosting provider:

| Concern | loopy depends on | Render | Fly / Railway | k8s | Bare VM |
|---|---|---|---|---|---|
| Secret | `LOOPY_ADMIN_TOKEN` from env | dashboard env var | `fly secrets` / env | Secret → env | env / `loopy.env` |
| TLS | terminated by the ingress | auto on `*.onrender.com` | auto | Ingress + cert-manager | nginx/caddy |
| Port | `$PORT` (fallback `--port`) | injected | injected | containerPort | flag/env |
| Persistence | `StateStore` (SQLite file) | persistent disk | volume | PVC | disk |

`GET /healthz` is open (liveness only, no data) for platform probes. The full design, including
the guardrails and the OIDC upgrade path, is in
[`docs/design/admin-auth.md`](docs/design/admin-auth.md); `loopy docs deployment` prints the
same contract offline.

## License

Loopy is open source under the [Apache License 2.0](LICENSE). You're free to use, modify, and
distribute it, including commercially; the license adds an express patent grant and asks that you
preserve attribution and note significant changes.
