"""`loopy init` scaffolding — the file templates for a fresh project.

Kept out of the CLI module so the (multi-line, brace-heavy) templates don't clutter the
command wiring, and so the scaffold is unit-testable as plain data. The canonical layout
mirrors `examples/codefix` (the smallest runnable loop): one `CodeTask` event → a
`claude-code` agent that edits a checkout and opens a PR. A freshly scaffolded project
compiles green out of the box — `tests/test_init_scaffold.py` guards that.
"""

from __future__ import annotations

import re
from pathlib import Path

# Sentinel replaced with the project name. Deliberately *not* `{name}`: these templates are
# full of literal braces (YAML inline maps, `{{ event.* }}` refs), so `str.format` would
# choke on them — a plain `.replace` of an unambiguous token is safer.
_NAME = "__PROJECT_NAME__"

# Sentinel for the BaseSandbox sandbox's `repos:` line — rendered from the repo(s) the user names at
# `init` time (or an empty list). Same `.replace` rationale as `_NAME`.
_REPOS_LINE = "__REPOS_LINE__"

# Sentinel for the top-level `public_url:` line — rendered from the hosted URL the user names at
# `init` time, or left as a commented stub to fill in later. Same `.replace` rationale as `_NAME`.
_PUBLIC_URL_LINE = "__PUBLIC_URL_LINE__"

# A project name doubles as the new directory name, so keep it a single safe path segment.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The only pre-existing entries `scaffold_project` tolerates in an otherwise-fresh target: the two
# files `loopy init` may write via `loopy auth github` before it scaffolds (see `scaffold_project`).
_PREEXISTING_OK = frozenset({"loopy.env", ".gitignore"})


class InvalidProjectName(ValueError):
    """The requested project name isn't a usable single directory segment."""


def validate_project_name(name: str) -> str:
    """Return the cleaned name, or raise `InvalidProjectName` with an actionable message."""
    cleaned = name.strip()
    if not cleaned:
        raise InvalidProjectName("project name must not be empty")
    if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise InvalidProjectName(f"{name!r} is not a valid directory name")
    if not _NAME_RE.match(cleaned):
        raise InvalidProjectName(
            f"{name!r} must start with a letter or digit and use only letters, digits, "
            "'.', '_' or '-'"
        )
    return cleaned


_REGISTRY_YML = """\
# __PROJECT_NAME__ — a starter Loopy project. One CodeTask event drives a claude-code agent
# that edits a checkout and opens a PR. Edit freely; `loopy compile .` validates the result.

# Where this loopy server is reachable from the internet — webhook senders (e.g. GitHub)
# deliver to <public_url>/hooks/<provider>. Edit any time; leave commented while unhosted.
__PUBLIC_URL_LINE__

# Defaults — every agent inherits these; override a field only when needed.
defaults:
  agent:
    sandbox: BaseSandbox

# Sandbox — compute + egress. `daytona` runs each agent in an isolated cloud sandbox built
# from the `image:` spec below (set DAYTONA_API_KEY in loopy.env). Swap `provider:` to `docker`
# for a fully local, hermetic run — both build from the same `image:` spec.
#   • `repos:` is what the agent clones to edit code — keep it pointed at a repo you can push to
#   • `env_file:` is the gitignored dotenv injected as the sandbox's environment
sandboxes:
  BaseSandbox:
    provider: daytona
    image: { debian_slim: "3.12", apt: [git], workdir: /home/loopy, user: loopy }
    # git over https + the model API. Switching a step to Codex (or OpenCode on an
    # OpenAI model)? Add api.openai.com here.
    network: [github.com, api.anthropic.com]
    env_file: secrets/base.env                  # gitignored; resolved at run time
    __REPOS_LINE__

# Agents — capability comes from the sandbox, skills, injected git creds, and budget.
# Every agent names both keys explicitly: `model` (what it runs on) and `harness` (the
# runner that drives it). One agent per supported harness; the starter workflow points at
# Claude. To run a step on another harness, change its `agent:` — and give the sandbox
# that provider's key (secrets/base.env) plus its API host (`network:` above).
agents:
  Claude:   { model: claude-sonnet-4-6, harness: claude-code, skills: [codefix] }
  Codex:    { model: gpt-5.5, harness: codex, skills: [codefix] }
  OpenCode: { model: claude-sonnet-4-6, harness: opencode, skills: [codefix] }

# Events — the bus contract. A step's `on:` may only name an event registered here.
events:
  # the task to act on — fire one by hand with `loopy trigger --event CodeTask --fields '{...}'`
  CodeTask:
    task: str     # what to change, in prose
    branch: str   # the branch to push the edit on
  # emitted once the PR is open — a downstream workflow could subscribe with `on: PROpened`
  PROpened:
    pr_url: url
    summary: str
"""

_OPEN_PR_MD = """\
---
on: CodeTask
agent: Claude
output:
  pr_url: url
  summary: str
emits: PROpened
budget: { wall_clock: 15, spend: { usd: 2 } }
---
A checkout of the target repository is already in your workspace — the sandbox cloned it at
startup, and a GitHub token is wired into git, so `git push` and PR creation just work.

Make the change described by the task: {{ event.task }}.

Then:
1. Create a new branch named `{{ event.branch }}`.
2. Commit your edit with a clear message.
3. Push the branch and open a pull request against the default branch. Include the loopy run
   id (the `$LOOPY_RUN_ID` environment variable) in the PR body so it can be traced to this run.

Return the pull request URL and a one-line summary of the change.
"""

_SKILL_MD = """\
# codefix

Make a focused code change in a checkout and open a pull request for it.

- Keep the diff minimal — change only what the task asks for; don't reformat untouched code.
- Match the surrounding style and conventions of the file you're editing.
- Use a descriptive branch name and a commit message that states the change, not the process.
- The PR body should say what changed and why, in a sentence or two.
- Don't push directly to the default branch; always open a PR from your branch.
"""

_SENSORS_PY = """\
from loopy import sensor
from loopy.events import CodeTask  # generated by `loopy compile` — optional, for your typechecker


@sensor(poll="10m", emits="CodeTask")  # `emits` is the contract the compiler reads
def task_queue(req) -> CodeTask:
    \"\"\"Poll a task queue for code-change requests.

    A stub to start from — a real sensor would read from Linear, a GitHub issue label, a
    spreadsheet, etc., and return a CodeTask per request. Returning None emits nothing; this
    is here so `CodeTask` has a declared producer (and you can still fire one by hand with
    `loopy trigger --event CodeTask`).
    \"\"\"
    return None
"""

_DEV_ENV = """\
# Secrets for the `BaseSandbox` sandbox, injected as environment variables at run time. Gitignored —
# never commit this file. Lines are KEY=VALUE; values are literal (no ${VAR} interpolation).

# --- model auth ---
# Required for the daytona/docker providers (the built image carries no creds). The Claude
# agent (claude-code) and the OpenCode agent's claude-* model authenticate with this key.
ANTHROPIC_API_KEY=sk-ant-...
# The Codex agent (and an OpenCode agent on an OpenAI model) uses this one instead.
# OPENAI_API_KEY=sk-...

# --- git auth ---
# Only needed if you are NOT using a GitHub App. `loopy auth github` injects a scoped token
# automatically; otherwise put a PAT with contents:write + pull_requests:write here.
# GITHUB_TOKEN=ghp_...

# --- only for the `local` provider (a bare subprocess inherits nothing from your shell) ---
# With the daytona/docker providers these come from the built image; leave them out.
# PATH=/usr/local/bin:/usr/bin:/bin
# HOME=/home/you
"""

_LOOPY_ENV = """\
# Control-plane credentials — the creds the loopy *engine* needs. These are NOT injected
# into sandboxes (the agent's environment is secrets/base.env); they stay at the control
# plane. Gitignored — never commit this file. Lines are KEY=VALUE; values are literal.

# --- GitHub App (recommended git auth) ---
# Leave these commented: `loopy auth github` writes GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY
# here for you. An uncommented GITHUB_APP_ID would make that command think an App is already
# configured and refuse to run.
# GITHUB_APP_ID=
# GITHUB_APP_PRIVATE_KEY=

# --- GitHub webhook ingress ---
# Verifies X-Hub-Signature-256 on /hooks/github deliveries. When a public URL is
# configured, `loopy auth github` registers the App's webhook and writes the
# GitHub-minted secret here for you. Wiring a webhook by hand instead? Generate a
# secret (openssl rand -hex 32), set it here, and give GitHub the same value.
# GITHUB_WEBHOOK_SECRET=

# --- Daytona (the default sandbox; required when a sandbox uses provider: daytona) ---
# DAYTONA_API_KEY=

# --- Redis event bus ---
# Set REDIS_URL to run on the networked Redis bus; `loopy run` picks it up automatically
# (leave it unset for the single-process in-memory bus). `--bus` overrides this.
# REDIS_URL=redis://localhost:6379
"""

_GITIGNORE = """\
# Generated by `loopy compile` (the loopy.events package)
/loopy/
# Compiled manifest
manifest.json

# Secrets — never commit
# control-plane creds incl. the GitHub App private key (loopy auth github)
loopy.env
# sandbox env_file(s)
secrets/
# sensor-layer secrets
sensors/.env

# Local run state (SQLite run-state DB written by `loopy run`)
.loopy/
"""

_README_MD = """\
# __PROJECT_NAME__

A Loopy project, scaffolded by `loopy init`. One `CodeTask` event drives a `claude-code`
agent that edits a checkout and opens a pull request.

## Run it

```bash
# 1. confirm sandboxes.BaseSandbox.repos points at a repo you can push to (set at init)

# 2. give the sandbox its secrets
#    edit secrets/base.env and set ANTHROPIC_API_KEY

# 3. wire git auth (writes loopy.env in this project — gitignored)
loopy auth github
loopy auth status            # then visit the printed install URL to pick repos

# 4. check the above is actually done — a green compile is not a runnable project
loopy doctor

# 5. start the engine — `loopy run` compiles the project on the fly, then brings up the
#    server (hosts sensors, drives runs; sandboxes run on whatever registry.yml's `provider:`
#    names). Container stack by default — add `--in-process` for a no-Docker dev server.
loopy run

# fire one task by hand to watch the loop end-to-end (compiles too):
loopy trigger . \\
  --event CodeTask \\
  --fields '{"task": "add a CONTRIBUTING.md", "branch": "codefix/contributing"}'
```

`run`/`trigger` accept a project directory and compile it for you; `loopy compile .` is still
there to write a standalone `manifest.json` (the deploy artifact) or as a CI gate (`--check`).
Run every command from this directory so `loopy.env` and `--root` stay in sync. See the
top-level Loopy README for the authoring model.
"""

# --- the no-repo starter: a non-coding workflow orchestrator -----------------------------------
# Scaffolded when `loopy init` is given no repo. A `Note` event drives an agent that distills it
# into a summary + action items — a genuinely useful loop on its own (and a clean template to
# swap a real source/skill into), with no git, no checkout, no GitHub App to set up.

_ORCH_REGISTRY_YML = """\
# __PROJECT_NAME__ — a starter Loopy project. One Note event drives a claude agent that distills
# it into a short summary + action items. No repo, no git — a pure workflow orchestrator. Edit
# freely; `loopy compile .` validates the result.

# Where this loopy server is reachable from the internet — webhook senders deliver to
# <public_url>/hooks/<path>. Edit any time; leave commented while unhosted.
__PUBLIC_URL_LINE__

# Defaults — every agent inherits these; override a field only when needed.
defaults:
  agent:
    sandbox: BaseSandbox

# Sandbox — compute + egress. `daytona` runs each agent in an isolated cloud sandbox built
# from the `image:` spec below (set DAYTONA_API_KEY in loopy.env). Swap `provider:` to `docker`
# for a fully local, hermetic run — both build from the same `image:` spec.
#   • no repos: this loop only talks to the model, so egress is just the model API
#   • want an agent that edits code? add a repo (and run `loopy auth github`) and a PR workflow
#   • `env_file:` is the gitignored dotenv injected as the sandbox's environment
sandboxes:
  BaseSandbox:
    provider: daytona
    image: { debian_slim: "3.12", workdir: /home/loopy, user: loopy }
    # just the model API — no git, no repos. Switching a step to Codex (or OpenCode on an
    # OpenAI model)? Add api.openai.com here.
    network: [api.anthropic.com]
    env_file: secrets/base.env                  # gitignored; resolved at run time

# Agents — capability comes from the sandbox, skills, and budget.
# Every agent names both keys explicitly: `model` (what it runs on) and `harness` (the
# runner that drives it). One agent per supported harness; the starter workflow points at
# Claude. To run a step on another harness, change its `agent:` — and give the sandbox
# that provider's key (secrets/base.env) plus its API host (`network:` above).
agents:
  Claude:   { model: claude-sonnet-4-6, harness: claude-code, skills: [summarize] }
  Codex:    { model: gpt-5.5, harness: codex, skills: [summarize] }
  OpenCode: { model: claude-sonnet-4-6, harness: opencode, skills: [summarize] }

# Events — the bus contract. A step's `on:` may only name an event registered here.
events:
  # the note to distill — fire one by hand with `loopy trigger --event Note --fields '{...}'`
  Note:
    text: str     # the content to summarize, in prose
  # emitted once the summary is ready — a downstream step could subscribe with `on: Summarized`
  Summarized:
    summary: str
    action_items: str
"""

_ORCH_WORKFLOW_MD = """\
---
on: Note
agent: Claude
output:
  summary: str
  action_items: str
emits: Summarized
budget: { wall_clock: 5, spend: { usd: 1 } }
---
You're handed a note. Distill it — no preamble, and don't just echo the input back.

The note:
{{ event.text }}

Produce two things:
1. `summary` — 2–3 sentences capturing the essence in plain language.
2. `action_items` — concrete next steps the note implies, one per line, each starting with a
   verb. If the note implies nothing to do, return the single line `none`.

Stay faithful to the note: don't invent facts, names, or action items it doesn't support.
"""

_ORCH_SKILL_MD = """\
# summarize

Turn a note into a short, faithful summary and a list of action items.

- Lead with a summary a busy reader could skim in one breath — 2–3 sentences, no filler.
- Pull out only action items the note actually implies: concrete, verb-first, one per line.
- Don't invent facts, names, or numbers that aren't in the note.
- If there's nothing to act on, say `none` rather than padding the list.
"""

_ORCH_SENSORS_PY = """\
from loopy import sensor
from loopy.events import Note  # generated by `loopy compile` — optional, for your typechecker


@sensor(poll="10m", emits="Note")  # `emits` is the contract the compiler reads
def notes_inbox(req) -> Note:
    \"\"\"Poll an inbox for notes to summarize.

    A stub to start from — a real sensor would read from email, a Slack channel, a webhook, or
    a spreadsheet, and return a Note per item. Returning None emits nothing; this is here so
    `Note` has a declared producer (and you can still fire one by hand with
    `loopy trigger --event Note`).
    \"\"\"
    return None
"""

_ORCH_DEV_ENV = """\
# Secrets for the `BaseSandbox` sandbox, injected as environment variables at run time. Gitignored —
# never commit this file. Lines are KEY=VALUE; values are literal (no ${VAR} interpolation).

# --- model auth ---
# Required for the daytona/docker providers (the built image carries no creds). The Claude
# agent (claude-code) and the OpenCode agent's claude-* model authenticate with this key.
ANTHROPIC_API_KEY=sk-ant-...
# The Codex agent (and an OpenCode agent on an OpenAI model) uses this one instead.
# OPENAI_API_KEY=sk-...

# --- only for the `local` provider (a bare subprocess inherits nothing from your shell) ---
# With the daytona/docker providers these come from the built image; leave them out.
# PATH=/usr/local/bin:/usr/bin:/bin
# HOME=/home/you
"""

_ORCH_LOOPY_ENV = """\
# Control-plane credentials — the creds the loopy *engine* needs. These are NOT injected
# into sandboxes (the agent's environment is secrets/base.env); they stay at the control
# plane. Gitignored — never commit this file. Lines are KEY=VALUE; values are literal.

# --- Daytona (the default sandbox; required when a sandbox uses provider: daytona) ---
# DAYTONA_API_KEY=

# --- Redis event bus ---
# Set REDIS_URL to run on the networked Redis bus; `loopy run` picks it up automatically
# (leave it unset for the single-process in-memory bus). `--bus` overrides this.
# REDIS_URL=redis://localhost:6379

# Adding a repo later? `loopy auth github` writes GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY here.
"""

_ORCH_README_MD = """\
# __PROJECT_NAME__

A Loopy project, scaffolded by `loopy init`. One `Note` event drives a `claude-code` agent that
distills it into a short summary and a list of action items — a pure workflow orchestrator, with
no code repo involved.

## Run it

```bash
# 1. give the sandbox its secrets
#    edit secrets/base.env and set ANTHROPIC_API_KEY

# 2. check it's runnable — a green compile is not a runnable project
loopy doctor

# 3. start the engine — `loopy run` compiles the project on the fly, then brings up the
#    server (hosts sensors, drives runs; sandboxes run on whatever registry.yml's `provider:`
#    names). Container stack by default — add `--in-process` for a no-Docker dev server.
loopy run

# fire one note by hand to watch the loop end-to-end (compiles too):
loopy trigger . \\
  --event Note \\
  --fields '{"text": "Customer call: they want SSO by Q3 and flagged slow CSV export."}'
```

Want an agent that edits code instead? Point `sandboxes.BaseSandbox.repos` at a repo you can
push to, run `loopy auth github`, and add a workflow that opens a PR.

`run`/`trigger` accept a project directory and compile it for you; `loopy compile .` is still
there to write a standalone `manifest.json` (the deploy artifact) or as a CI gate (`--check`).
Run every command from this directory so `loopy.env` and `--root` stay in sync. See the
top-level Loopy README for the authoring model.
"""


def _render_repos_line(repos: list[str]) -> str:
    """Render the BaseSandbox sandbox's `repos:` line from the repo(s) the agent will work on."""
    return f"repos: [{', '.join(repos)}]   # cloned at acquire time (git auth injected)"


def _render_public_url_line(public_url: str | None) -> str:
    """Render the top-level `public_url:` line — a real value, or a commented stub to fill in.

    The stub keeps the field visible in every fresh registry.yml, so "where is this hosted?"
    is always an edit away rather than a key the user has to discover.
    """
    if public_url:
        return f"public_url: {public_url}"
    return "# public_url: https://loopy.example.com"


def _coding_files(repos: list[str], public_url: str | None) -> dict[str, str]:
    """The repo-backed starter: a CodeTask → edit-a-checkout → open-a-PR loop."""
    registry = _REGISTRY_YML.replace(_REPOS_LINE, _render_repos_line(repos)).replace(
        _PUBLIC_URL_LINE, _render_public_url_line(public_url)
    )
    return {
        "registry.yml": registry,
        "workflows/codefix/open-pr.md": _OPEN_PR_MD,
        "skills/codefix/SKILL.md": _SKILL_MD,
        "sensors/sensors.py": _SENSORS_PY,
        "secrets/base.env": _DEV_ENV,
        "loopy.env": _LOOPY_ENV,
        ".gitignore": _GITIGNORE,
        "README.md": _README_MD,
    }


def _orchestrator_files(public_url: str | None) -> dict[str, str]:
    """The repo-less starter: a Note → summary + action-items loop (no git, no checkout)."""
    registry = _ORCH_REGISTRY_YML.replace(_PUBLIC_URL_LINE, _render_public_url_line(public_url))
    return {
        "registry.yml": registry,
        "workflows/summarize/summarize.md": _ORCH_WORKFLOW_MD,
        "skills/summarize/SKILL.md": _ORCH_SKILL_MD,
        "sensors/sensors.py": _ORCH_SENSORS_PY,
        "secrets/base.env": _ORCH_DEV_ENV,
        "loopy.env": _ORCH_LOOPY_ENV,
        ".gitignore": _GITIGNORE,
        "README.md": _ORCH_README_MD,
    }


def scaffold_project(
    target: Path, name: str, *, repos: list[str] | None = None, public_url: str | None = None
) -> list[Path]:
    """Write the starter project into `target`, returning the created files (relative paths).

    `target` is created if absent; an existing **non-empty** directory is refused so we never
    clobber work. The caller is expected to have validated `name` via `validate_project_name`.

    The starter is chosen by repo access — so a fresh project is something you'd actually keep,
    not a placeholder to fix. With one or more `repos`, you get the coding loop (edit a checkout,
    open a PR). With none, you get a repo-less orchestrator (turn a Note into a summary) — useful
    on its own, and never an unpushable placeholder repo.
    """
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    target = Path(target)
    # `loopy init` now offers `loopy auth github` *before* scaffolding, so a fresh target may
    # already hold the two files that step writes — loopy.env (App creds) and .gitignore. Tolerate
    # exactly those; any other pre-existing content is real work we refuse to clobber.
    if target.exists() and any(p.name not in _PREEXISTING_OK for p in target.iterdir()):
        raise FileExistsError(f"{target} already exists and is not empty")

    # Preserve any real control-plane creds a preceding `loopy auth github` wrote. The scaffold's
    # loopy.env ships commented placeholders, so we write the template first, then merge the real
    # values back on top — keeping both the explanatory comments and the creds.
    preexisting_creds = load_control_plane_env(target)

    repos = repos or []
    files = _coding_files(repos, public_url) if repos else _orchestrator_files(public_url)
    created: list[Path] = []
    for rel, template in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.replace(_NAME, name))
        created.append(Path(rel))
    if preexisting_creds:
        write_control_plane_env(target, preexisting_creds)
    return sorted(created, key=lambda p: p.as_posix())
