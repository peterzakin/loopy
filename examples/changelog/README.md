# changelog — a changelog entry per merged PR (built-in event, zero sensor code)

Keep a CHANGELOG in sync with what ships. When a pull request is merged, one agent drafts
the changelog entry it implies and opens a small PR that adds it, so the changelog never
falls behind the code. The [`../github/`](../github/) example reacts to a PR *opening*;
this one turns each *merge* into a documentation change.

| When | Workflow triggers on | Workflow | What the agent does |
| --- | --- | --- | --- |
| A PR is **merged** | `Github.PullRequestMerged` | `workflows/changelog/entry.md` | drafts the entry and opens a PR adding it |

## The trigger

`Github.PullRequestMerged` is a built-in event: the step names it in `on:`, and the
compiler registers the contract and synthesizes the `/hooks/github` sensor. No sensor
code and no `events:` entry for the inbound side. The GitHub App and webhook setup is the
same as the code review example — see [`../github/README.md`](../github/README.md#setup).

## The token

This workflow **writes**: it pushes a branch and opens a PR. Either run
`loopy auth github` (a GitHub App; the backend injects a short-lived, repo-scoped token
per step), or put a `GITHUB_TOKEN` with `contents:write` + `pull_requests:write` in
`secrets/base.env`.

## Run it

```
cp examples/changelog/base.env.example examples/changelog/secrets/base.env  # fill it in
loopy compile examples/changelog --out manifest.json
loopy run --in-process manifest.json
```

### Try it without GitHub

Drive the built-in event by hand with a sample payload:

```
loopy trigger examples/changelog --event Github.PullRequestMerged \
  --fields '{"number":7,"repo":"octocat/Hello-World","title":"Add widget","url":"https://github.com/octocat/Hello-World/pull/7","merged_by":"octocat"}'
```
