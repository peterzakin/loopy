# issue-triage — classify new GitHub issues (built-in event, zero sensor code)

When someone opens a GitHub issue, one agent reads it against the code, classifies it into
a **typed** area and severity, and posts a triage comment with a concrete next step. The
issue-side companion to the code review loop in [`../github/`](../github/).

| When | Workflow triggers on | Workflow | What the agent does |
| --- | --- | --- | --- |
| An issue is **opened** | `Github.IssueOpened` | `workflows/issue-triage/triage.md` | classifies area + severity, posts a triage comment |

## The trigger

`Github.IssueOpened` is a built-in event: the step names it in `on:`, and the compiler
registers the contract and synthesizes the `/hooks/github` sensor. No sensor code and no
`events:` entry for the inbound side. The GitHub App and webhook setup is the same as the
code review example — see [`../github/README.md`](../github/README.md#setup) (when
registering webhooks by hand, subscribe **Issues**).

## Typed classification

The `IssueTriaged` event's `enum[...]` fields double as the classification vocabulary the
agent must choose from, so the label is validated at run time, not free text. Swap the
enum members in `registry.yml` to match your own labels.

## Run it

```
cp examples/issue-triage/base.env.example examples/issue-triage/secrets/base.env  # fill it in
loopy compile examples/issue-triage --out manifest.json
loopy run --in-process manifest.json
```

### Try it without GitHub

Drive the built-in event by hand with a sample payload:

```
loopy trigger examples/issue-triage --event Github.IssueOpened \
  --fields '{"number":42,"repo":"octocat/Hello-World","title":"Crash on empty input","body":"Passing an empty string raises IndexError.","author":"octocat","url":"https://github.com/octocat/Hello-World/issues/42"}'
```
