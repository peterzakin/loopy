# Loopy — the authoring reference

Loopy is an open-source, agent-neutral framework for authoring agent automations. Your
automations are *files in your repo* (workflows, skills, and sensors authored as Markdown
and code), so they version, diff, and review like the rest of your codebase. There's no
canvas to click together: `loopy compile` builds the workflow's DAG straight from those
files.

**Agent-neutral.** Every step names its runtime in `registry.yml` (`harness.runtime`):
**Claude Code**, **OpenAI Codex**, and **OpenCode** ship today. Mix them in one manifest:
route a triage step to one runtime and a fixer to another, and swap a step's runtime or
model without touching its prose.

This is the reference for *authoring* a project. For the diagnostic-code catalog, run
`loopy docs errors`. Examples live at
<https://github.com/peterzakin/loopy/tree/main/examples>.

## The verify loop

```bash
loopy compile --check .   # validate: DAG, registry refs, {{ }} templates. Exit 0 = valid.
loopy doctor              # runnable? placeholder keys, git auth, provider creds. Exit 0 = ready.
loopy trigger . --event <EventName> --fields '{"field": "value"}' --json   # one run, end-to-end
```

A green compile is **not** a runnable project: compile proves the manifest is well-formed,
`loopy doctor` proves the credentials and repos behind it are real.

## Workflows

A **workflow** is a directory. Inside it:

- Exactly **one entry step** carries `on:`, either a single **registered** Event or a
  built-in time trigger `on: cron("<expr>")`. A step triggers on **exactly one** event;
  fan-in from many sources is done at the **sensor layer** (several sensors emit one
  normalized event, e.g. `Incident`).
- **Every other step** carries `after: <step>` (or `after: [a, b]`), consuming the
  predecessor's **outputs**.
- Data flows by reference: `{{ event.field }}` (from the triggering event) or
  `{{ step.field }}` (an **output** of a step you're `after`). There is no `ref()`.
- Any step may also `emits:` a **registered** event for other workflows to subscribe to.

The engine builds the DAG directly: the `on:` step is the root, `after:` edges are the
order. A step with neither `on:` nor `after:` is an orphan, a compile-time error.

`cron("<expr>")` takes a **quoted** 5-field cron expression (optional `, tz=...`); the
quotes keep commas in the expression (`cron("1,15 * * * *")`) from colliding with the
`tz=` separator. It's built in, so it needs no registry entry. The step receives a tick as
its event, with `{{ event.scheduled_at }}` and `{{ event.last_run }}` so it can scan only
what changed since it last ran.

## Layout

```
ProjectName/
  registry.yml                  # reused, Capitalized entities: Agents · Sandboxes · Events
  workflows/                    # each subdirectory is one single-entry workflow
    triage/      investigate.md                          # on: Incident → WorkItem
    upkeep/      scan-deps.md                            # on: cron("0 3 * * *") → WorkItem
    resolve/     arbitrate.md · fix.md · review.md · ship.md
  skills/                       # reusable agent skills, referenced by name from registry.yml
    triage/             SKILL.md
    rubrics/fix-quality/ SKILL.md                        # namespaced
  sensors/                      # the event-publish layer: code that emits registered events
    sensors.py
```

## Naming convention

Defined entities are **Capitalized types** (`WorkItem`, `Investigator`, `MetricThreshold`),
and references point at them by that name (`on: WorkItem`, `agent: Investigator`).
Filenames, step names, and event *fields* (`event.issue_id`) stay lowercase.

## A step file

```markdown
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

- **Outputs** are a step's structured data results, declared on the step (`output:`, a
  typed field map). A downstream step in the **same** workflow consumes them with
  `after:` + `{{ step.field }}`. Outputs are **not** events; they never touch the bus.
- **Events** are emitted (`emits:`) onto the shared bus for **other** workflows to
  subscribe to (`on:`). Events must be formally **registered** in `registry.yml`; the bus
  only routes registered events, and an `on:` trigger requires its event to exist in the
  registry. Sensors publish events too; `on:` doesn't care whether a sensor or a step
  emitted it.

The test for which to reach for: *does another workflow need this value?* If the next
step in the same workflow consumes it, it's an **output**. If another workflow subscribes
to it (or a step needs to loop back to a workflow's entry), it's an **event**.

## The registry

`registry.yml` holds the reused entities, defined once and referenced by name:

```yaml
# Defaults: every agent inherits these; override a field only when needed.
# `harness.runtime` picks the agent runner: `claude-code` (Claude Code, claude-* models),
# `codex` (OpenAI Codex, gpt-*/o-series/codex-* models), or `opencode` (OpenCode, which
# drives either family — write the bare model id; loopy expands it to opencode's
# provider/model naming). Model must match the runtime.
defaults:
  agent:
    sandbox: default
    harness: { runtime: claude-code, model: claude-sonnet-4-6 }

# Sandbox: compute + egress. `image:` is the declarative build; `network:` the egress
# allowlist. `env_file:` points at a gitignored dotenv (a path, or a list of paths merged
# in order) whose keys are injected as the sandbox's environment (the sandbox inherits
# *nothing* from your shell; secrets like `ANTHROPIC_API_KEY` must live here). `repos:`
# are cloned into the workspace at acquire time, with git auth injected.
sandboxes:
  default:
    provider: daytona
    image: { debian_slim: "3.12", apt: [git], workdir: /home/loopy, user: loopy }
    network: [github.com, api.anthropic.com]   # git over https + the model API
    env_file: secrets/base.env        # gitignored; injected as the sandbox's env
    repos: [octocat/Hello-World]      # cloned into the workspace at acquire time

# Agents: capability comes from the sandbox (image + egress), skills, injected creds, and
# budget; numeric caps live in budget, not in a tool name.
agents:
  Investigator: { skills: [triage, repro-authoring] }                  # inherits default harness
  Fixer:        { harness: { model: claude-opus-4-8 }, skills: [testing] }
  Reviewer:     { skills: [rubrics/fix-quality] }
  Scout:        { harness: { runtime: codex, model: gpt-5 }, skills: [triage] }
  Sweeper:      { harness: { runtime: opencode, model: claude-sonnet-4-6 }, skills: [triage] }

# Events: the bus contract. A step's `on:` may only name an event registered here.
events:
  Incident:        { source: enum[sentry, linear, datadog, pagerduty, slack], issue_id: str, title: str, link: url }
  MetricThreshold: { goal_id: str }
  WorkItem:
    source:        enum[sentry, linear, datadog, pagerduty, slack, cve]
    link:          url
    root_cause:    str
    proposed_goal: str
    repro:         str
  GoalShipped:     { goal_id: str }
```

**No built-in agents.** Every agent a step names must be declared in `registry.yml` — the
harness pairing is always explicit and visible, never injected.

Beyond a single step's `budget:`, the registry takes a top-level `limits:` block for
wider spend caps: `cascade_spend: { usd: <n> }` caps the total spend of an entire event
cascade, and `workflows: { <Name>: { spend: { usd: <n> } } }` caps one named workflow.

### Event field types

An event's fields are a `name: type` map. Loopy has no type system of its own: a field
type is just a **JSON Schema** fragment (draft 2020-12), validated at runtime by pydantic
and code-generated into the `loopy.events` classes. The terse forms below are authoring
sugar over that.

| Authored | Meaning | JSON Schema emitted |
|---|---|---|
| `str` | string | `{"type": "string"}` |
| `int` | integer | `{"type": "integer"}` |
| `float` | number | `{"type": "number"}` |
| `bool` | boolean | `{"type": "boolean"}` |
| `url` | string, URI-formatted | `{"type": "string", "format": "uri"}` |
| `enum[a, b, c]` | one of a fixed set of strings | `{"type": "string", "enum": ["a", "b", "c"]}` |

The same field types apply to a step's `output:` map. An unknown bare shorthand (not in
the table above and not a valid schema object) is a compile-time error (`LOOPY-E201`).

## Skills

Reusable agent skills, a sibling of `workflows/`. One directory per skill (a `SKILL.md`
plus any resources), and agents reference them **by name** in `registry.yml`
(`skills: [triage, rubrics/fix-quality]`). Define a skill once, reuse it across agents.
Namespaced subdirectories are allowed (`rubrics/fix-quality`). A skill name resolves only
against `skills/`; an unresolved name is a compile-time error.

The organizing principle: **`registry.yml` holds the lightweight, inline config entities**
(Agents, Sandboxes, Events; a few fields each), while **top-level directories hold the
authored artifacts that have a *body*** (`workflows/`, `skills/`, and `sensors/`).

## Sensors

The event-publish layer: code that turns the outside world into **registered events**.
One or more files (a single `sensors.py` is fine); each sensor is a function decorated
with `@sensor`, triggered by a `poll` or a `webhook`.

**Common GitHub events are built in; you may not need a sensor at all.** A workflow can
trigger on a platform-shipped event directly (`on: Github.PullRequestOpened`) with no
`registry.yml` entry and no `sensors/` file; the compiler injects the contract and a
`/hooks/github` sensor for you. Catalog: `Github.PullRequestOpened`, `PullRequestMerged`,
`IssueOpened`, `IssueCommentCreated`, `Push`. The `Github.` namespace is reserved.

A sensor **returns a registered event**, and returning *is* emitting: the event goes on
the bus and routes to whichever workflow subscribes with `on:`. Return `None` to emit
nothing, or `yield` an `Iterator[Event]` to emit several.

**Compile rule.** A sensor must **declare** the event it emits via `emits=` (a registered
event from `registry.yml`), in a form `loopy compile` can read statically; it never
imports or runs your code. A sensor that declares no `emits`, names an unregistered
event, or builds its declaration imperatively fails to load before anything runs. That's
what guarantees every event on the bus has a contract. The return type (`-> Incident`) is
optional: it's checked by *your* typechecker (mypy) against `loopy.events`, not by the
compiler.

```python
from loopy import sensor
from loopy.events import Incident             # generated from registry.yml; optional

@sensor(webhook="/hooks/sentry", emits="Incident")   # `emits` is the contract the compiler reads
def sentry_issues(req) -> Incident:
    i = req.json["data"]["issue"]
    return Incident(source="sentry", issue_id=i["id"], title=i["title"], link=i["permalink"])
```

Sensors are **Python-only today**: the compiler statically inspects every `.py` under
`sensors/` (subdirectories included) and nothing else.

**Poll intervals are plain durations.** `@sensor(poll="5m")` takes a whole number plus a
unit (`s`, `m`, `h`, or `d`: `"30s"`, `"1h"`, `"2d"`); a malformed interval is a
compile-time error (`LOOPY-E403`). Ticks never overlap, and a tick's watermark advances
only after its events are delivered, so a failed poll re-covers the same window and skips
no data. The very first tick asks for exactly one interval of history. To run on a clock
rather than a gap, use `cron(...)` on a workflow's `on:` instead.

**Webhooks.** `loopy run` hosts each `@sensor(webhook=...)` as an HTTP route and fans one
path out to every sensor on it (GitHub posts every event type to a single URL, so several
sensors can share `/hooks/github`). The public delivery URL for a sensor is one base plus
its path: set `LOOPY_PUBLIC_URL` in `loopy.env` (prompted for at `loopy init`) to your
deployed host or dev tunnel; `loopy webhooks list` prints every endpoint's full delivery
URL (so does `loopy run` at startup) — the strings to paste into a source's webhook
settings. For GitHub there's nothing to paste: **`loopy webhooks github`** registers the
webhooks for you — it creates (or converges, idempotently) a webhook on each repo in
`registry.yml` pointing at `<LOOPY_PUBLIC_URL>/hooks/github`, subscribed to the events
your sensors need, authenticated as the App from `loopy auth github` (auth offers this
step itself when the URL is already set; `--check` reports without changing anything).
It also generates and lands `GITHUB_WEBHOOK_SECRET` in `loopy.env`, and when that secret
is set, `loopy run` verifies GitHub's `X-Hub-Signature-256` HMAC at the edge before any
sensor sees the payload. Without registration (or the manual equivalent), built-in
`Github.*` triggers never fire — `loopy doctor` flags exactly that gap.

## Secrets

- **The sandbox inherits nothing from your shell.** Everything an agent needs (the model
  key, any git token) must be in the sandbox's `env_file`; exporting `ANTHROPIC_API_KEY`
  in your shell is not enough. (The bare `local` provider also needs `PATH`/`HOME` there;
  `docker`/`daytona` get those from the image.)
- `secrets/base.env` (or whatever `env_file:` names) is the sandbox env: model keys
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) and `GITHUB_TOKEN` if no GitHub App is
  configured. Gitignored.
- `loopy.env` at the project root holds control-plane creds the *engine* needs
  (`DAYTONA_API_KEY`, the GitHub App entries `loopy auth github` writes). Never injected
  into sandboxes. Gitignored.
- **Where an agent runs is authored in `registry.yml`, not on the command line.** Every
  sandbox must declare its `provider:` (`local | docker | daytona`); there is no
  `--sandbox` flag.

## Running

- **`loopy compile <path>` writes `manifest.json` by default** (`--out` changes the path;
  `--check` validates without writing, the CI gate). It also generates a gitignored
  `loopy/` events package under the project (`loopy/events.py` + stubs, for your
  typechecker). There's no separate compile step in the dev loop: `loopy run` compiles a
  directory target and recompiles a stale manifest automatically.
- **`loopy trigger`** fires one event and runs the cascade to completion in-memory (the
  test path; not recorded). `--json` emits the full run record: steps, outputs, emitted
  events, failures. A hand-fired event with no sensor producing it warns
  `LOOPY-W501 dead trigger` — expected for the manual-trigger pattern; the run proceeds.
- **`loopy run`** brings up the containerized stack (a `redis` bus container plus the
  engine); `loopy run --in-process` is the no-Docker dev server, recording every run to
  `.loopy/state.db`.
- **`loopy admin`** serves a read-only dashboard over that DB (run list, step timeline,
  emitted events, outputs, failures) at <http://127.0.0.1:9000>. `loopy demo` serves the
  same views against in-memory sample data.
- **`loopy auth github`** creates a GitHub App via the manifest flow (browser required)
  and writes its creds to `loopy.env`; tokens are then minted and injected into sandboxes
  on both `run` and `trigger`. Headless alternative: a `GITHUB_TOKEN` with
  `contents:write` + `pull_requests:write` in the sandbox's `env_file`.
