# dep-upkeep — a nightly dependency-bump loop

A scheduled maintenance loop in the spirit of Dependabot: every night at 3am, one agent
finds outdated or vulnerable dependencies and opens a single pull request that bumps them.

```
cron("0 3 * * *") ─▶ dep-upkeep/bump (Upkeeper) ─▶ DepsBumped
                     finds outdated deps · edits manifests · opens a PR
```

Where [`standup`](../standup/) is a read-only scheduled loop, this one **writes**: the agent
edits the repo, pushes a branch, and opens a PR. So it needs git write access, either a
GitHub App (`loopy auth github`) or a `GITHUB_TOKEN` with `contents:write` +
`pull_requests:write`.

## What it shows

- **cron → edit → PR**, the scheduled counterpart to [`codefix`](../codefix/)'s
  event-driven edit. Same "agent edits a checkout and opens a PR" shape, armed by the clock.
- **Ecosystem-agnostic prose.** The objective names the manifests to look for and lets the
  agent pick the right tool per ecosystem, rather than hard-coding one.
- **A no-op is valid.** If nothing is outdated, the agent opens no PR and returns
  `packages: 0`.

## Run it

> From a checkout of this repo, prefix each command with `uv run`.

```bash
# 1. point registry.yml's repos: at a repo you can push to
# 2. wire git auth (App or token) and set ANTHROPIC_API_KEY in secrets/base.env
cp examples/dep-upkeep/base.env.example examples/dep-upkeep/secrets/base.env
loopy auth github          # or put a GITHUB_TOKEN in secrets/base.env

# 3. compile
loopy compile examples/dep-upkeep

# 4. start the engine; the scheduler fires the workflow at each 3am tick
loopy run
```

A cron workflow fires on its schedule under `loopy run`; there is no hand-fired cron trigger
(`loopy trigger` fires *events*, and a cron entry doesn't subscribe to one). To watch it fire
without waiting for 3am, set the expression to a near time while testing, e.g.
`on: cron("*/5 * * * *")`, then run `loopy run` and watch it in `loopy admin`.
