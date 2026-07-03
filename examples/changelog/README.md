# changelog — keep a CHANGELOG in sync with what ships

When a pull request is merged, one agent drafts the changelog entry it implies and opens a
small PR that adds it, so the changelog never falls behind the code.

```
Github.PullRequestMerged ─▶ changelog/entry (Scribe) ─▶ ChangelogUpdated
                            drafts the entry · opens a PR adding it
```

The trigger is the **built-in** `Github.PullRequestMerged` event (no `events:` entry, no
sensor). The [`github`](../github/) example reacts to the same merge event by proposing
follow-on work; this one turns it into a documentation change. This workflow **writes**, so
it needs git write access (a GitHub App via `loopy auth github`, or a `GITHUB_TOKEN` with
`contents:write` + `pull_requests:write`).

## What it shows

- **A built-in merge event driving an edit + PR.** The whole loop is one step: read the
  merged diff, write the entry, open the PR.
- **Match-the-repo behavior.** The agent adapts to the repo's existing changelog format (or
  creates one), rather than imposing a fixed template.
- **A no-op is valid.** Purely internal changes open no PR.

## Run it

> From a checkout of this repo, prefix each command with `uv run`.

See the [`github` example README](../github/README.md) for the one-time GitHub App + webhook
setup. Then:

```bash
cp examples/changelog/base.env.example examples/changelog/secrets/base.env
#   set ANTHROPIC_API_KEY and wire git auth (App or token)
loopy compile examples/changelog
loopy run                      # hosts /hooks/github; merge a PR to see it fire
```
