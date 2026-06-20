# codefix — a minimal repo-editing loop you can run locally

The smallest *real* Loopy workflow: one `CodeTask` event drives a `claude-code` agent that
edits a checkout, pushes a branch, and opens a pull request.

```
CodeTask ─▶ codefix/open-pr  (Coder agent)  ─▶ PROpened
            clones repo · edits · pushes branch · opens PR
```

The canonical `incidents` example is multi-workflow and assumes the Daytona + `loopy run`
happy path. This one fills the other gap: a **single repo-touching step you can drive
end-to-end on a laptop with `loopy trigger`** — the path where the toolchain, `HOME`, and
credentials actually have to be wired up. If you're new to Loopy, read the top-level
[`README.md`](../../README.md) first for the authoring model; this doc is about *running*.

## Layout

```
codefix/
  registry.yml                 # Dev sandbox · Coder agent · CodeTask + PROpened events
  workflows/codefix/open-pr.md # on: CodeTask → edit + open PR → emits PROpened
  skills/codefix/SKILL.md       # how the agent should make the change
  sensors/sensors.py            # a stub poll producer for CodeTask (fire by hand instead)
  dev.env.example               # template for the gitignored secrets/dev.env
```

## Run it locally

> Running from a checkout of this repo, `loopy` isn't on your PATH — prefix each command
> below with `uv run` (e.g. `uv run loopy compile examples/codefix`).

### 0. Pick the target repo

`registry.yml`'s `Dev` sandbox clones `octocat/Hello-World` at startup (the `repos:` field).
**Point it at a repo you can push to** — fork something, or change `repos:` to `you/your-repo`.
Repos are static today (no `{{ event.repo }}` templating yet), so the repo lives in the spec
and the *task* rides the event.

### 1. Compile

```bash
loopy compile examples/codefix    # writes manifest.json
```

A green compile is the gate: every `on:`/`emits:` names a registered event and every `{{ }}`
ref resolves.

### 2. Give the sandbox its secrets

Agents need a model key and git auth. Where they come from depends on the sandbox provider —
**this is the part the `incidents` example glosses over**, because tokens are *not*
auto-injected unless you configure a GitHub App (below), and the bare `local` sandbox inherits
*nothing* from your shell.

Copy the template to the gitignored path the registry references:

```bash
mkdir -p examples/codefix/secrets
cp examples/codefix/dev.env.example examples/codefix/secrets/dev.env
# then edit secrets/dev.env
```

> **These are live secrets on disk.** `env_file` values are read literally (no `${VAR}`
> interpolation — that's deliberate: one file tells you exactly what the sandbox sees), so
> `secrets/dev.env` holds a real `ANTHROPIC_API_KEY` in cleartext. `.gitignore` is the only
> thing keeping it out of a commit — `loopy init` and this example's layout gitignore
> `secrets/` for you, so keep your env_file under that path and never `git add` it. `loopy
> doctor` warns if it sees an env_file tracked by git; if it does, untrack it with
> `git rm --cached <path>`. Prefer the GitHub App for git auth (below) so no token lands here
> at all, and Claude OAuth on `local` so no model key does either.

What the `env_file` must supply, by the sandbox's `provider:` (set in `registry.yml`):

| Var | `provider: docker` (default) | `provider: local` (no Docker) |
|-----|------------------------------|-------------------------------|
| `ANTHROPIC_API_KEY` | **required** (the image has no creds) | required, unless Claude OAuth creds are reachable via `HOME` — see below |
| `GITHUB_TOKEN` | required *unless* a GitHub App is configured (see below) | same |
| `PATH` | not needed — comes from the image | **required** — else `claude`/`git` aren't found |
| `HOME` | not needed — comes from the image | **required** — for `~/.gitconfig` and `~/.claude/.credentials.json` |

> **Why `PATH`/`HOME` for `local`?** The `local` provider runs the agent as a bare subprocess
> with *only* the `env_file` as its environment — your shell's `PATH`/`HOME` are not inherited
> (this was the #1 papercut for the first real run). The `docker` provider avoids it entirely:
> the toolchain, `PATH`, and `HOME` come from the image, so isolation matches Daytona while
> needing only a local Docker daemon. **Prefer `provider: docker`.**

> **OAuth instead of an API key (`local`):** if you use Claude Code via a subscription, point
> `HOME` at a directory containing `~/.claude/.credentials.json` and you can omit
> `ANTHROPIC_API_KEY` — the `claude-code` harness treats the model key as satisfied when OAuth
> creds are reachable through the sandbox's own `HOME`.

#### Git auth: a GitHub App (recommended) or a token in the env_file

The clean path is a **bring-your-own GitHub App** — run it once and tokens are minted and
injected (scoped, short-lived) into the sandbox on *both* `loopy run` and `loopy trigger`:

```bash
loopy auth github          # App Manifest flow; ~2 clicks; writes .loopy/ + loopy.env
loopy auth status          # verify, then visit the printed install URL to pick repos
```

With an App configured you can leave `GITHUB_TOKEN` out of `dev.env` entirely. Without one,
put a token (`contents:write` + `pull_requests:write`) in `dev.env` as `GITHUB_TOKEN` — the
sandbox's git is wired to read it for `github.com`. Pass `--no-tokens` to skip minting for a
fully offline test.

### 3. Trigger one task

```bash
loopy trigger manifest.json \
  --event CodeTask \
  --fields '{"task": "add a CONTRIBUTING.md with a one-line build command", "branch": "codefix/contributing"}' \
  --root examples/codefix
```

The sandbox runs on whatever `registry.yml`'s `provider:` names (here, `docker`).

It fires the event, runs the single step to completion, and prints the step order, the emitted
`PROpened` event, and the step's outputs — including the `pr_url`. Add `--json` for the full
run record (steps, outputs, emits, failures).

> **Watching runs:** `loopy trigger` is the one-shot test path and runs in-memory, so it isn't
> recorded. To see runs in the dashboard, drive the workflow through the server instead —
> `loopy run --in-process manifest.json` records to `.loopy/state.db`, and `loopy admin` (in another terminal,
> at http://127.0.0.1:9000) shows the run list, timeline, and outputs read-only.

## One-command smoke test

`smoke.sh` drives the whole thing end-to-end against a throwaway repo (compile → trigger →
assert a PR/branch appears). It's the live counterpart to the offline CI test and needs real
creds:

```bash
TARGET_REPO=you/sandbox-repo ./examples/codefix/smoke.sh
```

The **always-on, no-creds** smoke test is `tests/conformance/test_codefix.py`: it compiles
this example and runs the same `CodeTask` cascade on the offline stub harness (no model key,
no token, no network), so CI catches structural breakage on every push. Run it with:

```bash
uv run pytest tests/conformance/test_codefix.py -q
```
