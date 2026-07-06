# customer-feedback-loop — from a Zendesk ticket to a pull request

A Zendesk ticket arrives. A **webhook sensor** shapes it into a typed `CustomerTicket`,
and one agent decides whether the ticket points at real work in the codebase. When it
does, the agent opens a pull request that links back to the ticket; when it is a
question, a duplicate, or nothing actionable, it does nothing. Most tickets are not code
changes, so the workflow's first job is to tell the two apart — a how-to question costs a
read and nothing else.

```
Zendesk ──POST /hooks/zendesk──▶ zendesk_tickets() ──▶ CustomerTicket ──▶ entry.md ──▶ PR (or nothing)
```

| Piece | File | What it does |
| --- | --- | --- |
| Sensor | `sensors/sensors.py` | shapes each new-ticket payload into a `CustomerTicket` |
| Workflow | `workflows/customer-feedback/entry.md` | judges the ticket; opens a PR only when it warrants work |

## The sensor

`@sensor(webhook="/hooks/zendesk", emits="CustomerTicket")` registers the route;
**returning is emitting**. Run `loopy webhooks list` to get the full URL, then point a
Zendesk webhook + trigger (fired on ticket creation) at it with a JSON body carrying the
ticket's id, subject, description, and url.

## The token

This workflow **writes** when a ticket warrants it: a branch and a pull request. Either
run `loopy auth github` (a GitHub App; the backend injects a short-lived, repo-scoped
token per step), or put a `GITHUB_TOKEN` with `contents:write` + `pull_requests:write`
in `secrets/base.env`.

## Run it

```
cp examples/customer-feedback-loop/base.env.example examples/customer-feedback-loop/secrets/base.env
loopy compile examples/customer-feedback-loop --out manifest.json
loopy run --in-process manifest.json
```

### Try it without Zendesk

Fire the event by hand with a sample ticket:

```
loopy trigger examples/customer-feedback-loop --event CustomerTicket \
  --fields '{"ticket_id":"1234","subject":"Export button does nothing","body":"Clicking Export on the reports page has no effect.","link":"https://acme.zendesk.com/agent/tickets/1234"}'
```
