# issue-triage — auto-triage a newly opened issue

When someone opens a GitHub issue, one agent reads it, classifies it, and posts a triage
comment (area, severity, and a concrete next step) back on the issue.

```
Github.IssueOpened ─▶ issue-triage/triage (Triager) ─▶ IssueTriaged
                      classifies the issue · posts a comment via gh
```

This is the issue-side companion to the [`github`](../github/) example's PR review. Like it,
the trigger is a **built-in** `Github.*` event: the workflow triggers on
`Github.IssueOpened` directly, and the compiler injects the event contract and a
`/hooks/github` sensor. No `events:` entry and no sensor code for the inbound side.

## What it shows

- **A built-in event with no boilerplate.** `on: Github.IssueOpened` needs no declaration.
- **Typed enums as the label vocabulary.** The `IssueTriaged` event's `area` and `severity`
  are `enum[...]`, so the agent's classification is validated against a fixed set, not free
  text. Swap the enum members to match your own labels.
- **Reading the code to classify.** The agent triages against the checkout, not the issue
  text alone.

## Run it

> From a checkout of this repo, prefix each command with `uv run`.

See the [`github` example README](../github/README.md) for the one-time GitHub App + webhook
setup that delivers `Github.*` events. Then:

```bash
cp examples/issue-triage/base.env.example examples/issue-triage/secrets/base.env
#   set ANTHROPIC_API_KEY (and wire git auth for the gh comment)
loopy compile examples/issue-triage
loopy run                      # hosts /hooks/github; open an issue to see it fire
```
