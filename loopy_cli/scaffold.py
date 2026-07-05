"""`loopy init` scaffolding — the file templates for a fresh project.

Kept out of the CLI module so the (multi-line, brace-heavy) templates don't clutter the
command wiring, and so the scaffold is unit-testable as plain data. The canonical layout
mirrors `examples/github` (the smallest event-driven loop): the built-in
`Github.PullRequestOpened` event → a `claude-code` agent that reviews the opened PR's diff
and posts review comments. A freshly scaffolded project compiles green out of the box —
`tests/test_init_scaffold.py` guards that.

Two shapes, chosen by GitHub access. With a repo, the review workflow is scaffolded live. With
none (a path `loopy init` strongly discourages), the *same* workflow ships **disabled** — as
`workflows/review/code-review.md.disabled`, a name the discovery glob (`workflows/*/*.md`)
skips so the project still compiles green — and every entry file points at wiring GitHub
access, then renaming the file to enable it.
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

# Sentinel for the starter-specific tail of AGENTS.md (what the scaffolded workflow does and
# how to fire it — or, for the minimal no-repo scaffold, what's missing and how to close it).
_STARTER_BLOCK = "__STARTER_BLOCK__"

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
# __PROJECT_NAME__ — the default loop: a pull request is opened → a claude-code agent reviews
# the diff and posts review comments. Docs: `loopy docs`. Validate edits: `loopy compile .`.

defaults:
  agent:
    sandbox: BaseSandbox

sandboxes:
  BaseSandbox:
    provider: daytona                          # or `docker` for a local run
    image: { debian_slim: "3.12", apt: [git, gh], workdir: /home/loopy, user: loopy }
    env_file: secrets/base.env             # injected as the sandbox's env (e.g. ANTHROPIC_API_KEY)
    __REPOS_LINE__

agents:
  Claude:   { model: claude-opus-4-8, harness: claude-code }
  Codex:    { model: gpt-5.5, harness: codex }
  OpenCode: { model: claude-sonnet-4-6, harness: opencode }

# events: the review loop triggers on the built-in `Github.PullRequestOpened` — its contract
# and a sensor on /hooks/github are injected by the compiler, so there's nothing to declare
# here. Register an event only when you add a workflow that `emits:` one for another to consume.
"""

# The default workflow — mirrors examples/github/workflows/review/code-review.md. Shipped live
# in the repo-backed scaffold and, byte-for-byte identical, as the `.disabled` file in the
# no-repo scaffold (enabling it there is a rename, not a rewrite).
_REVIEW_MD = """\
---
on: Github.PullRequestOpened
agent: Claude
output:
  verdict: str
  comments_posted: int
budget: { wall_clock: 15, spend: { usd: 2 } }
---
A checkout of {{ event.repo }} is already in your workspace, with a GitHub token wired into
git and the `gh` CLI — so fetching branches and posting review comments just work.

Review pull request #{{ event.number }} — "{{ event.title }}" — which merges branch
`{{ event.branch }}` into `{{ event.base }}`.

1. Fetch the PR branch and produce the diff against `{{ event.base }}`.
2. Review it for correctness bugs, security issues, and clear simplifications. Focus on
   high-confidence findings; don't nitpick style.
3. Post each finding as an inline review comment on {{ event.url }} via `gh` (the injected
   token authenticates you). If it's clean, leave a single approving summary comment.

Return your overall verdict (e.g. "approve" or "changes requested") and the number of
comments you posted.
"""

_DEV_ENV = """\
# The sandbox's environment, injected at run time. Gitignored. KEY=VALUE, literal values.
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...     # for Codex, or OpenCode on an OpenAI model
# GITHUB_TOKEN=ghp_...      # git auth if not using a GitHub App (contents + PR write)
"""

_LOOPY_ENV = """\
# Control-plane credentials — the engine's, not the sandbox's (that's secrets/base.env).
# Gitignored. `loopy init`/`loopy auth github` fill these in; leave them commented.
# GITHUB_APP_ID=
# GITHUB_APP_PRIVATE_KEY=
# DAYTONA_API_KEY=                    # required for provider: daytona
# REDIS_URL=redis://localhost:6379    # networked bus; unset = in-memory
# LOOPY_PUBLIC_URL=                   # public base URL for inbound webhooks
# LOOPY_ADMIN_TOKEN=                  # gates /admin; minted by `loopy init`
"""

_GITIGNORE = """\
# Compile outputs
/loopy/
manifest.json

# Secrets — never commit (control-plane creds, sandbox env_files, sensor secrets)
loopy.env
secrets/
sensors/.env

# Local run state (SQLite DB written by `loopy run`)
.loopy/
"""

_README_MD = """\
# __PROJECT_NAME__

A Loopy project, scaffolded by `loopy init`. The default loop reacts to the built-in
`Github.PullRequestOpened` event: when a pull request is opened, a `claude-code` agent reviews
the diff and posts review comments on it.

## Run it

```bash
loopy auth github            # wire git auth + the App that delivers PR events (writes loopy.env)
# then set ANTHROPIC_API_KEY in secrets/base.env

loopy doctor                 # a green compile is not a runnable project — check creds/repos
loopy webhooks github        # register the PR webhook on your repo(s) (needs LOOPY_PUBLIC_URL)
loopy run                    # start the engine (compiles first); --in-process = no-Docker

# no open PR handy? drive the loop by hand with a sample payload:
loopy trigger . --event Github.PullRequestOpened \\
  --fields '{"number": 1, "repo": "octocat/Hello-World", "title": "Add widget", \\
             "branch": "feat/widget", "base": "main", \\
             "url": "https://github.com/octocat/Hello-World/pull/1"}'
```

Run every command from this directory. [`AGENTS.md`](AGENTS.md) is the one-page map a coding
agent reads first (authoring rules, the verify loop, the secrets model).
"""

# --- AGENTS.md — the agent-facing map, shared by both scaffolds --------------------------------
# Most first edits to a scaffolded project are made by a coding agent ("build me a loop that…"),
# and AGENTS.md is the file those agents auto-discover. It compresses the authoring model, the
# verify loop, and the secrets rules into one page so an agent can work without fetching docs.

_AGENTS_MD = """\
# AGENTS.md — working in this Loopy project

This directory is a **Loopy project**: agent automations authored as files. Workflows,
skills, and sensors are Markdown and Python; `registry.yml` declares the shared entities
(Agents, Sandboxes, Events); `loopy compile` builds the workflow DAG straight from these
files. This page is the map. `loopy docs` prints the full authoring reference and
`loopy docs errors` explains every `LOOPY-E` diagnostic code.

## The edit loop

Validate after every change — the compiler is fast, static, and precise:

```bash
loopy compile --check .   # exit 0 = valid. Errors: `error LOOPY-E### file:line:col: message`
loopy doctor              # exit 0 = runnable. Names what's missing (keys, git auth, provider creds)
```

**A green compile is not a runnable project.** Compile proves the files are well-formed;
`loopy doctor` proves the credentials and repos behind them are real. Run it before the
first run and after touching any env file.

To exercise one run end-to-end (in-memory, no server needed):

```bash
loopy trigger . --event <EventName> --fields '{"field": "value"}' --json
```

`--json` prints the full run record: steps, outputs, emitted events, failures.

## Layout

| Path | What it is |
|---|---|
| `registry.yml` | the shared entities: agents, sandboxes, events (a few fields each) |
| `workflows/<name>/` | one workflow per directory, one step per `.md` file |
| `skills/<name>/SKILL.md` | reusable skills; agents reference them by name in `registry.yml` |
| `sensors/` | `@sensor` functions that turn outside signals into registered events |
| `secrets/base.env` | the sandbox's env (model key, git token) — gitignored |
| `loopy.env` | control-plane creds (Daytona key, GitHub App); never reaches a sandbox |
| `manifest.json`, `loopy/` | compile outputs — generated, don't hand-edit |

## Rules the compiler enforces

- A workflow has exactly **one entry step** carrying `on:` — a registered event or
  `on: cron("0 3 * * *")` (quoted 5-field expression). Every other step carries
  `after: <step>` (or `after: [a, b]`). A step with neither is an orphan (E103).
- **Outputs vs. events.** `output:` is a step's typed result, read by later steps in the
  *same* workflow as `{{ step.field }}`. `emits:` puts a registered event on the bus for
  *other* workflows to `on:`. Same-workflow handoff = output; cross-workflow seam = event.
- Every event in `on:`/`emits:` must be registered under `events:` in `registry.yml` (E504).
  Exception: built-in `Github.*` triggers (`PullRequestOpened`, `PullRequestMerged`,
  `IssueOpened`, `IssueCommentCreated`, `Push`) need no registration and no sensor.
- Field types are JSON Schema shorthand: `str`, `int`, `float`, `bool`, `url`,
  `enum[a, b, c]`. Anything else is E201.
- Every step's `agent:` must exist in `registry.yml` (E501); every sandbox declares
  `provider: local | docker | daytona` (E214); every agent resolves to a sandbox (E506).
- Defined entities are **Capitalized** (`ReviewPosted`, `Claude`) and referenced by exact
  name. Filenames, step names, and event *fields* stay lowercase.
- A sensor declares its event statically — `@sensor(poll="10m", emits="EventName")` or
  `@sensor(webhook="/hooks/x", emits="EventName")`. The compiler reads the decorator and
  never runs your code (E402). Poll intervals: `"30s"`, `"5m"`, `"1h"`, `"2d"`.
- Templates flow by reference: `{{ event.field }}` from the trigger, `{{ step.field }}`
  from a direct `after:` predecessor. Unresolvable refs fail compile (E302/E304/E305).

## A step file

```markdown
---
on: SomeEvent               # entry step only; other steps use `after: <step>`
agent: Claude               # must exist in registry.yml
output: { result: str }     # typed; downstream steps read {{ this_step.result }}
emits: SomethingHappened    # optional — only if another workflow subscribes
budget: { wall_clock: 15, spend: { usd: 2 } }   # wall_clock in minutes
---
The agent's objective, in prose. Reads {{ event.field }} from the trigger.
```

## Secrets (the #1 first-run trap)

The sandbox inherits **nothing** from the shell. Everything the agent needs lives in the
file the sandbox's `env_file:` names (`secrets/base.env`): `ANTHROPIC_API_KEY` for
claude-code, `OPENAI_API_KEY` for codex, `GITHUB_TOKEN` for git (unless a GitHub App is
configured). `loopy.env` holds engine-side creds and never reaches a sandbox. Both are
gitignored — never commit them, and never write a secret into `registry.yml` or a step.

## Commands

| Command | What it does |
|---|---|
| `loopy compile --check .` | validate only (the CI gate); bare `compile` writes `manifest.json` |
| `loopy doctor` | runnability check — placeholder keys, git auth, provider creds |
| `loopy trigger . --event E --fields '{...}' --json` | fire one event; print the run record |
| `loopy run` | start the engine (compiles first); `--in-process` = no-Docker dev server |
| `loopy admin` | read-only dashboard over recorded runs (http://127.0.0.1:9000) |
| `loopy auth github` | GitHub App manifest flow — **opens a browser; not headless-safe** |
| `loopy webhooks github` | register GitHub repo webhooks (`loopy webhooks list` = delivery URLs) |
| `loopy docs [topic]` | this reference in full; `loopy docs errors` = the diagnostic catalog |

Headless notes: `loopy init` and `loopy auth github` are interactive — both need a human
on a terminal (auth also opens a browser), so ask the user to run them. For git auth a
`GITHUB_TOKEN` (contents:write + pull_requests:write) in `secrets/base.env` works instead.

## This project's starter

__STARTER_BLOCK__
"""

_CODING_STARTER_BLOCK = """\
One workflow, `review`: the built-in `Github.PullRequestOpened` event drives the `Claude`
agent to review the opened PR's diff (in a checkout of the repo(s) in
`sandboxes.BaseSandbox.repos`) and post review comments on it. Live GitHub deliveries need
the webhook registered (`loopy webhooks github`); until then, drive it by hand:

```bash
loopy trigger . --event Github.PullRequestOpened \\
  --fields '{"number": 1, "repo": "octocat/Hello-World", "title": "Add widget", \\
             "branch": "feat/widget", "base": "main", \\
             "url": "https://github.com/octocat/Hello-World/pull/1"}' --json
```\
"""

_MINIMAL_STARTER_BLOCK = """\
The default workflow — a PR is opened → the `Claude` agent reviews the diff and posts comments
— ships **disabled** as `workflows/review/code-review.md.disabled`. This project was
initialized **without GitHub access**, and the review loop needs a repo to check out and a
GitHub token to post with, so nothing runs until you close that gap:

1. Run `loopy auth github` (needs a human and a browser — ask the user to run it, or put a
   `GITHUB_TOKEN` in `secrets/base.env`).
2. Add the repo(s) to `sandboxes.BaseSandbox.repos` in `registry.yml`.
3. Rename `workflows/review/code-review.md.disabled` to `code-review.md` (dropping the
   `.disabled` suffix), then `loopy compile .`. The compiler picks it up as a live workflow.\
"""


# --- the no-repo scaffold: the review workflow, shipped disabled --------------------------------
# Scaffolded when `loopy init` is given no repo — a path init strongly discourages. The default
# review workflow still ships, but disabled (`code-review.md.disabled`, which the discovery glob
# skips), because it needs a repo to check out and a GitHub token to post with. Every file points
# at the same next steps: wire GitHub access, name the repo, then rename the file to enable it.

_MINIMAL_REGISTRY_YML = """\
# __PROJECT_NAME__ — the default review loop (a PR is opened → an agent reviews it) ships
# DISABLED here (no GitHub access yet): see workflows/review/code-review.md.disabled.
# Enable it: `loopy auth github`, uncomment `repos:` below, drop the file's `.disabled` suffix.

defaults:
  agent:
    sandbox: BaseSandbox

sandboxes:
  BaseSandbox:
    provider: daytona                          # or `docker` for a local run
    image: { debian_slim: "3.12", apt: [git, gh], workdir: /home/loopy, user: loopy }
    env_file: secrets/base.env                   # supplies these keys in local dev
    env: [ANTHROPIC_API_KEY]                     # forwarded from the platform env in production
    # repos: [owner/repo]

agents:
  Claude:   { model: claude-opus-4-8, harness: claude-code }
  Codex:    { model: gpt-5.5, harness: codex }
  OpenCode: { model: claude-sonnet-4-6, harness: opencode }

# events: the review loop triggers on the built-in `Github.PullRequestOpened` — nothing to
# declare. Register an event only when you add a workflow that `emits:` one for another to consume.
"""

_MINIMAL_README_MD = """\
# __PROJECT_NAME__

A Loopy project, scaffolded by `loopy init` **without GitHub access**. The default loop — a
pull request is opened → a `claude-code` agent reviews the diff and posts comments — ships
**disabled** as `workflows/review/code-review.md.disabled`, because the review loop needs a
repo to check out and a GitHub token to post with.

## Enable the default loop

```bash
loopy auth github            # wire git auth + the App that delivers PR events (writes loopy.env)
# then, in registry.yml: uncomment BaseSandbox `repos:` and name your repo(s),
# and set ANTHROPIC_API_KEY in secrets/base.env

mv workflows/review/code-review.md.disabled workflows/review/code-review.md
loopy compile --check .      # the compiler now picks up the workflow; validate it
loopy doctor                 # a green compile is not a runnable project — check what's missing
```

Then `loopy webhooks github` registers the PR webhook and `loopy run` starts the engine.
[`AGENTS.md`](AGENTS.md) is the one-page map a coding agent reads first.
"""


def _render_repos_line(repos: list[str]) -> str:
    """Render the BaseSandbox sandbox's `repos:` line from the repo(s) the agent will work on."""
    return f"repos: [{', '.join(repos)}]   # cloned at run time (git auth injected)"


def _coding_files(repos: list[str]) -> dict[str, str]:
    """The repo-backed starter: a Github.PullRequestOpened → review-the-diff → post-comments loop.
    """
    return {
        "registry.yml": _REGISTRY_YML.replace(_REPOS_LINE, _render_repos_line(repos)),
        "workflows/review/code-review.md": _REVIEW_MD,
        "secrets/base.env": _DEV_ENV,
        "loopy.env": _LOOPY_ENV,
        ".gitignore": _GITIGNORE,
        "README.md": _README_MD,
        "AGENTS.md": _AGENTS_MD.replace(_STARTER_BLOCK, _CODING_STARTER_BLOCK),
    }


def _minimal_files() -> dict[str, str]:
    """The no-repo scaffold: the review workflow shipped disabled, plus the registry + env files.

    The workflow ships as `code-review.md.disabled` — a name the discovery glob skips — so the
    project compiles green while the default loop waits for GitHub access to be wired.
    """
    return {
        "registry.yml": _MINIMAL_REGISTRY_YML,
        "workflows/review/code-review.md.disabled": _REVIEW_MD,
        "secrets/base.env": _DEV_ENV,
        "loopy.env": _LOOPY_ENV,
        ".gitignore": _GITIGNORE,
        "README.md": _MINIMAL_README_MD,
        "AGENTS.md": _AGENTS_MD.replace(_STARTER_BLOCK, _MINIMAL_STARTER_BLOCK),
    }


def scaffold_project(
    target: Path, name: str, *, repos: list[str] | None = None
) -> list[Path]:
    """Write the starter project into `target`, returning the created files (relative paths).

    `target` is created if absent; an existing **non-empty** directory is refused so we never
    clobber work. The caller is expected to have validated `name` via `validate_project_name`.

    The scaffold is chosen by repo access. With one or more `repos`, the review workflow is
    scaffolded live (a PR is opened → an agent reviews the diff and posts comments). With none —
    a path `loopy init` strongly discourages — the same workflow ships disabled
    (`code-review.md.disabled`, a name discovery skips, so it stays out of compile) and every
    file points at wiring GitHub access, then renaming it to enable, as the next step.
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
    files = _coding_files(repos) if repos else _minimal_files()
    created: list[Path] = []
    for rel, template in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.replace(_NAME, name))
        created.append(Path(rel))
    if preexisting_creds:
        write_control_plane_env(target, preexisting_creds)
    return sorted(created, key=lambda p: p.as_posix())
