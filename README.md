<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/images/logo-dark.svg">
    <img src=".github/images/logo-light.svg" alt="Loopy" width="360">
  </picture>
</p>

<p align="center">
  <em>Agent workflows that run when your data changes.</em>
</p>

<p align="center">
  open source &middot; agent neutral &middot; code-first
</p>

<p align="center">
  <a href="https://pypi.org/project/loopy-computer/"><img src="https://img.shields.io/pypi/v/loopy-computer.svg?label=pypi&color=2563eb" alt="PyPI version"></a>
  <a href="https://pypi.org/project/loopy-computer/"><img src="https://img.shields.io/pypi/pyversions/loopy-computer.svg?color=2563eb" alt="Python versions"></a>
  <a href="https://pepy.tech/projects/loopy-computer"><img src="https://img.shields.io/pepy/dt/loopy-computer?color=2563eb" alt="Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-2563eb.svg" alt="License: Apache-2.0"></a>
</p>

---

Loopy is an open-source, agent-neutral framework for authoring agent automations. Workflows,
skills, and sensors live in your repo as Markdown and code, so they version, diff, and review
like the rest of your codebase. There is no canvas to click together: `loopy compile` builds
the workflow DAG straight from those files.

## Installation

```bash
uv tool install loopy-computer   # puts `loopy` on your PATH
```

> **Tip:** Working from a checkout of this repo? `uv tool install .` from the repo root
> installs your local source. Prefer not to install at all? Prefix commands with `uv run`
> (e.g. `uv run loopy compile`).

## Quickstart

```bash
loopy init my-project            # interactive wizard: scaffold, credentials, webhooks
cd my-project

loopy compile --check            # is the project valid?
loopy doctor                     # is it runnable? (names anything still missing)
loopy trigger --json             # fire one run end-to-end, full record on stdout

loopy run --in-process .         # dev server: compiles, hosts sensors, records every run
loopy admin                      # in another terminal → http://127.0.0.1:9000
```

A project is a directory, and its credentials live inside it: `secrets/base.env` (the sandbox
environment) and `loopy.env` (control-plane credentials), both gitignored. `loopy init` sets
all of this up and finishes by running the same checks as `loopy doctor`, so you know exactly
what's left before a first run.

## Why Loopy?

- **Files, not canvases.** Workflows are Markdown files with YAML frontmatter; the DAG is
  compiled from `on:`/`after:` edges. Everything versions, diffs, and reviews like the rest of
  your codebase.
- **Agent-neutral.** Every agent names its `harness`, the runner that drives it. Claude
  Code, OpenAI Codex, and OpenCode ship today, and the harness registry is built to
  take more. Mix them in one manifest: route triage to one harness and a fixer to another, and
  swap a step's harness or model without touching its prose.
- **Typed contracts.** Events and step outputs are typed field maps (JSON Schema under the
  hood, validated at runtime by pydantic and code-generated into `loopy.events` for your
  typechecker).
- **Sandboxed execution.** Every agent runs in a declared sandbox (`local`, `docker`, or
  `daytona`) with an explicit image, environment, and cloned repos. The sandbox inherits
  *nothing* from your shell.
- **Budgets and limits.** Per-step `budget:` (wall clock, spend), plus registry-level caps for
  a whole workflow or an entire event cascade.
- **Built to be driven by agents.** `loopy docs` prints the full authoring reference as
  markdown straight from the CLI, and every check is exit-code-clean, so a coding agent can
  drive the whole authoring cycle headlessly.
- **Observability included.** Runs are recorded to a durable store; `loopy admin` serves a
  dashboard of run timelines, emitted events, outputs, and failures, locally or against a
  hosted control plane, bearer-token-gated end to end.

## How it works

A **workflow** is a directory of steps. Exactly one entry step carries `on:` (a registered
event, or a built-in `cron("<expr>")` trigger); every other step carries `after: <step>` and
consumes its predecessors' outputs. A step is one Markdown file:

```yaml
---
on:     CustomerTicket                  # what triggers the step (a registered event, or a cron schedule)
agent:  SupportEngineer                 # who runs it, from registry.yml
output: { pr_url: url, verdict: str }   # typed result, read by later steps as {{ step.field }}
---
A customer opened Zendesk ticket {{ event.ticket_id }}: "{{ event.subject }}".

{{ event.body }}

Decide whether this ticket points at real work in this codebase.

1. Search the code for the behavior the ticket describes.
2. If it is a bug or a small feature you can address, implement the change on a branch
   and open a pull request that links back to {{ event.link }}. If it is a question, a
   duplicate, or not actionable in code, do nothing.
3. Return the PR URL (empty if you skipped) and a one-line verdict.
```

The reusable entities (Agents, Sandboxes, and Events) are defined once in
`registry.yml` and referenced by name:

```yaml
defaults:
  agent: { sandbox: BaseSandbox, model: claude-sonnet-4-6, harness: claude-code }

sandboxes:
  BaseSandbox:
    provider: daytona
    image: { debian_slim: "3.12", apt: [git, gh], workdir: /home/loopy, user: loopy }
    env_file: secrets/base.env       # gitignored; injected as the sandbox's env
    repos: [octocat/Hello-World]     # cloned into the workspace, git auth injected

agents:
  SupportEngineer: {}                # judges the ticket, opens a PR when it warrants work

events:
  CustomerTicket: { ticket_id: str, subject: str, body: str, link: url }   # inbound, from the Zendesk sensor
```

A project lays out like this:

```
my-project/
  registry.yml              # reused, Capitalized entities: Agents · Sandboxes · Events
  workflows/                # each subdirectory is one single-entry workflow
    customer-feedback/  entry.md        # on: CustomerTicket, opens a PR when warranted
  sensors/                  # the event-publish layer: code that emits registered events
    sensors.py              # the Zendesk webhook that shapes tickets into CustomerTicket
```

**Outputs vs. events.** Within a workflow, data flows by reference along `after:` chains: a
later step reads an earlier step's typed result as `{{ step.field }}`. Those are **outputs**,
and they never touch the bus. **Events** are for crossing it: the Zendesk sensor puts a
`CustomerTicket` on the bus and the workflow picks it up with `on:`, and a step can put one
there too with `emits:`, for another workflow to consume. The test: *does another workflow
need this value?*

**Sensors** turn the outside world into registered events: a decorated function triggered by
a `poll` interval or a `webhook` route, where returning is emitting:

```python
from loopy import sensor
from loopy.events import CustomerTicket   # generated from registry.yml

# webhook: Zendesk POSTs a new ticket; you shape it into a CustomerTicket
@sensor(webhook="/hooks/zendesk", emits="CustomerTicket")
def zendesk_tickets(req) -> CustomerTicket:
    ticket = req.json["ticket"]
    return CustomerTicket(ticket_id=ticket["id"], subject=ticket["subject"],
                          body=ticket["description"], link=ticket["url"])
```

Common GitHub and Sentry events are built in: a workflow can trigger on
`on: Github.PullRequestOpened` or `on: Sentry.IssueCreated` with no registry entry and no
sensor at all, and `loopy webhooks github` registers GitHub's side for you. A bare trigger
fires for every registered repo; scope it with a filter:
`on: Github.PullRequestOpened(repo="octocat/Hello-World")`. Webhook signatures
(`X-Hub-Signature-256`, `Sentry-Hook-Signature`) are verified at the edge before any sensor
sees the payload.

## Examples

Real workflows, wired together by events, authored as files in a repo, not clicked together
on a canvas. Each one has a step-by-step walkthrough at
[loopy.computer/examples](https://loopy.computer/examples.html):

**Scheduled loops.** Time-driven workflows. The entry step names a built-in `cron(...)`
schedule, so there is no sensor to write. Each tick carries `last_run`, so the agent acts
only on what changed since then.

| Example | What it shows |
|---|---|
| [Dependency upkeep](https://loopy.computer/example-dep-upkeep.html) | Every night, an agent finds outdated or vulnerable dependencies and opens a single pull request that bumps them. |

**GitHub built-ins.** Workflows that trigger on built-in `Github.*` events. No sensor code
and no event declaration: the step names the event in `on:`, and the compiler wires the
webhook.

| Example | What it shows |
|---|---|
| [Code review](https://loopy.computer/example-github-review.html) | A pull request opens. `Github.PullRequestOpened` fires, and an agent reviews the diff and posts inline comments on the PR. |
| [Issue triage](https://loopy.computer/example-issue-triage.html) | An issue opens. `Github.IssueOpened` fires, and an agent classifies it into a typed area and severity, then posts a triage comment. |
| [Changelog](https://loopy.computer/example-changelog.html) | A pull request merges. `Github.PullRequestMerged` fires, and an agent drafts the changelog entry it implies and opens a PR adding it. |

**Event-driven loops.** A sensor turns an outside signal into a typed event, and only when
it matters. The event on the bus drives a workflow that has no idea where it came from.

| Example | What it shows |
|---|---|
| [Uptime](https://loopy.computer/example-uptime.html) | A poll sensor checks a health endpoint and emits an `Incident` only when it is down. A second workflow reacts by opening a GitHub issue. |
| [Customer feedback product loop](https://loopy.computer/example-customer-feedback-loop.html) | A webhook sensor turns each new Zendesk ticket into a `CustomerTicket`. A workflow decides whether it warrants work and opens a pull request when it does. |

## Deployment

Run the engine yourself with Docker, deploy it from a git push to Render or Railway, or
provision a starter stack on AWS. The agents run in Daytona either way, so you deploy only the
small **engine**: `loopy run` is one long-lived process that loads the manifest, hosts your
sensor webhooks, and drives runs as events arrive. Each step's agent runs in a Daytona sandbox
you reach with an API key, so that compute is never yours to host. Full walkthroughs, with the
env split for every credential, are in `loopy docs deployment`.

**Run the stack yourself with Docker.** `loopy compile` turns the project into one
`manifest.json`, and a green compile is the deploy gate, so run it in CI. Put the engine's infra
credentials in `loopy.env` at the project root (`DAYTONA_API_KEY`, `LOOPY_PUBLIC_URL`, and the
`GITHUB_WEBHOOK_SECRET` that `loopy webhooks github` writes), then `loopy run` brings up the
stack: a `redis` container (the event bus) and the `loopy` container (sensor webhooks plus the
runtime), with run history on a named volume so both survive a restart.

```bash
cd my-project
loopy compile .          # writes manifest.json; a red compile is meant to fail the deploy
loopy run --detach       # brings up redis + loopy; webhooks on :8000
```

**Deploy from a git push (Render, Railway, Fly).** Any platform that builds a container from a
git push runs the engine the same way. `loopy dockerfile` writes a version-pinned `Dockerfile`
and `.dockerignore` that run `loopy compile` during the build (so a red compile fails the
deploy), and `loopy env` prints the control-plane environment to paste into the platform's
settings. Add the platform's managed Redis and point `REDIS_URL` at it so the queue survives a
restart, give the engine a persistent disk at your `--state-path` so run history survives a
redeploy, and keep it at one instance (SQLite is a single writer). The sandbox model key never
enters the image: it rides the sandbox `env_file`, which `.dockerignore` keeps out of the build,
so provide it as the platform's secret file at the same project-relative path `registry.yml`
names.

**Provision a starter stack on AWS.** `loopy deploy bootstrap` stands up one CloudFormation
stack in your account: a single EC2 instance running the same `redis` + `loopy` pair, behind a
CloudFront distribution that terminates TLS on its own `*.cloudfront.net` name. GitHub and
Sentry deliver webhooks over HTTPS with no domain to bring and no DNS to touch, and an EBS
volume keeps run history across a reboot. loopy provisions the host for you, so there is no load
balancer, managed Redis, or certificate to manage.

```bash
loopy deploy bootstrap --region us-east-1            # prints the https://<id>.cloudfront.net URL when it is live
loopy deploy bootstrap --destroy --region us-east-1  # tears it down; snapshot the volume first to keep run history
```

Whichever path you pick, point your sources at the public URL last: `loopy webhooks github`
registers the GitHub webhooks against `LOOPY_PUBLIC_URL` for you, and `loopy webhooks list`
prints every other sensor's full delivery URL to paste into that service's settings.

### Watch runs from your dev machine

The dashboard is read-only, but run and step outputs carry diffs, links, and root-cause text,
so a hosted control plane serves them only behind auth. The engine's one public URL is
path-routed: webhooks arrive at `$LOOPY_PUBLIC_URL/hooks/*` and the dashboard mounts at
`$LOOPY_PUBLIC_URL/admin`, so there is no second service to deploy. Copy the `LOOPY_ADMIN_TOKEN`
that `loopy init` minted in `loopy.env` into the platform's environment, then run `loopy admin`
locally: it serves the UI at `127.0.0.1:9000` and proxies data from `$LOOPY_PUBLIC_URL/admin`
over HTTPS with the bearer token attached, so your browser never holds it.

```bash
loopy admin   # UI at 127.0.0.1:9000, data from $LOOPY_PUBLIC_URL/admin over HTTPS
```

On the bootstrap stack, CloudFront reaches the instance over plain HTTP, so `/admin` is refused
at the edge; `loopy admin` reaches it over an SSM tunnel it prints for you instead.

## Documentation

The full reference ships inside the CLI, version-matched to your install and readable
offline (or by an agent):

```bash
loopy docs              # the full authoring reference: workflows, registry, sensors, secrets
loopy docs deployment   # hosting the control plane: env vars, $PORT, TLS, admin auth
loopy docs errors       # the stable LOOPY-E diagnostic catalog
```

The verify loop is exit-code-clean (`loopy compile --check` → `loopy doctor` →
`loopy trigger --json`), so a coding agent can author and validate a project end to end.
The only two commands that need a human are `loopy init` (interactive wizard) and
`loopy auth github` (browser flow).

## License

Loopy is open source under the [Apache License 2.0](LICENSE). You're free to use, modify, and
distribute it, including commercially; the license adds an express patent grant and asks that
you preserve attribution and note significant changes.
