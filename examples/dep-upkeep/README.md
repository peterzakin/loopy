# dep-upkeep — a scheduled maintenance loop (cron, zero sensor code)

A loop in the spirit of Dependabot: every night, one agent finds outdated or vulnerable
dependencies in the repo and opens a **single pull request** that bumps them. One step,
one agent, and no sensor — the trigger is the built-in `cron(...)` schedule.

| When | Workflow triggers on | Workflow | What the agent does |
| --- | --- | --- | --- |
| Nightly at 3am | `cron("0 3 * * *")` | `workflows/dep-upkeep/bump.md` | bumps outdated deps and opens one PR |

## The trigger

`cron("<expr>")` is a built-in time trigger: name it in the entry step's `on:` and the
scheduler fires the workflow on that 5-field cron expression — no sensor module and no
`events:` entry. Each tick exposes exactly two template fields:

- `{{ event.scheduled_at }}` — this tick's timestamp (the step uses it to name a unique
  branch per run);
- `{{ event.last_run }}` — the previous tick, so a step can act only on what changed
  since then.

## The token

This workflow **writes**: it pushes a branch and opens a PR. Either run
`loopy auth github` (a GitHub App; the backend injects a short-lived, repo-scoped token
per step), or put a `GITHUB_TOKEN` with `contents:write` + `pull_requests:write` in
`secrets/base.env`.

## Run it

```
cp examples/dep-upkeep/base.env.example examples/dep-upkeep/secrets/base.env  # fill it in
loopy compile examples/dep-upkeep --out manifest.json
loopy run --in-process manifest.json
```

The scheduler arms the cron trigger and fires it at 03:00. To watch a run without waiting
for the clock, temporarily set the expression to `cron("* * * * *")` (every minute) and
recompile. Inspect runs with `loopy admin` (dashboard at http://127.0.0.1:9000).
