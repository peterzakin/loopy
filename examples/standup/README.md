# standup — a scheduled digest loop

The smallest **time-driven** Loopy project: every weekday at 9am, one agent reads what
merged in the repo since it last ran and writes a short standup digest.

```
cron("0 9 * * 1-5") ─▶ standup/digest (Digester) ─▶ DigestReady
                       reads merged PRs since last run · writes the digest
```

Where [`codefix`](../codefix/) is triggered by an event you fire, this one is triggered by
the clock. There's no sensor to write and no webhook to host: `on: cron(...)` is built in,
and each tick hands the step `{{ event.last_run }}` and `{{ event.scheduled_at }}` so it
digests exactly the window since the previous run and never double-counts.

## What it shows

- **A `cron(...)` entry step.** A quoted 5-field expression (`"0 9 * * 1-5"` = weekdays at
  9am), no `events:` entry required.
- **The watermark.** `{{ event.last_run }}` is the previous tick's time, so the agent scans
  only what changed. A failed tick re-covers its window on the next run.
- **A typed output that could fan out.** The step emits `DigestReady { markdown, pr_count }`;
  a second workflow could subscribe with `on: DigestReady` to post it to Slack or email.

## Run it

> From a checkout of this repo, prefix each command with `uv run`.

```bash
# 1. point registry.yml's repos: at a repo you can read (default: octocat/Hello-World)

# 2. give the sandbox its secrets
cp examples/standup/base.env.example examples/standup/secrets/base.env
#    then edit secrets/base.env and set ANTHROPIC_API_KEY

# 3. compile (writes manifest.json; the DAG prints)
loopy compile examples/standup

# 4. start the engine; the scheduler fires the workflow at each 9am tick
loopy run
```

A cron workflow fires on its schedule under `loopy run`; there is no hand-fired cron trigger
(`loopy trigger` fires *events*, and a cron entry doesn't subscribe to one). To watch it fire
without waiting for 9am, set the expression to a near time while testing, e.g.
`on: cron("*/5 * * * *")`, then run `loopy run` and watch it in `loopy admin`.
