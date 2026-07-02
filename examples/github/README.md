# github — react to GitHub events (built-in, zero sensor code)

Two loops driven by GitHub deliveries to a single webhook URL — and **neither needs a sensor
or an event declaration**. The workflow just names a built-in `Github.*` event in its `on:`:

| When | Workflow triggers on | Workflow | What the agent does |
| --- | --- | --- | --- |
| A PR is **opened** | `Github.PullRequestOpened` | `workflows/review/code-review.md` | reviews the diff and posts review comments |
| A PR is **merged** | `Github.PullRequestMerged` | `workflows/scan/find-work.md` | proposes the follow-on work the PR implies |

When a workflow triggers on a `Github.*` event, the compiler registers that event's contract
and synthesizes a sensor on `/hooks/github` for you. GitHub posts *every* event type to that one
URL, so the runner verifies the delivery signature once at the edge and fans it out to the
built-in sensors; only the matching event emits (a PR `opened` vs `closed`+merged).

The full built-in catalog is `Github.PullRequestOpened`, `Github.PullRequestMerged`,
`Github.IssueOpened`, `Github.IssueCommentCreated`, and `Github.Push`. Trigger on any of them
the same way. (The `Github.` namespace is reserved — you can't declare your own event or write a
sensor in it; if you need an event the catalog doesn't cover, author a normal sensor as usual.)

## How it fits together

```
GitHub ──webhook(/hooks/github, X-Hub-Signature-256)──▶ loopy ingress
   verify HMAC  ─▶  fan out to built-in sensors  ─▶  Github.PullRequestOpened ─▶ code-review
                                                  └▶  Github.PullRequestMerged ─▶ find-work
                         (each step runs in a sandbox with a scoped GitHub token injected)
```

- **Inbound trust:** the webhook secret verifies `X-Hub-Signature-256` (`scm/github_webhook.py`).
- **Outbound trust:** `loopy auth github` configures a GitHub App; the runtime mints a
  short-lived, repo-scoped installation token per step and injects it into the sandbox — no
  static PAT crosses the boundary (`scm/token_provider.py`).

> Scope note: a built-in event fires for **any** repository the GitHub App delivers — there is no
> per-repo filter on the trigger. Scope is whatever the App is installed on (here, the repo in
> `sandboxes.BaseSandbox.repos`).

## Setup

1. **Public URL** — set the top-level `public_url:` in `registry.yml` to the base URL where
   this loopy server is reachable (a fresh `loopy init` asks for it and scaffolds the field):
   ```yaml
   public_url: https://<your-host>
   ```
   The webhook payload URL is `<public_url>/hooks/github`; `loopy run` prints it at startup.
   (For local dev, expose `http://127.0.0.1:8000` with a tunnel, e.g. `cloudflared`/`ngrok`,
   and use the tunnel URL.)
2. **GitHub App for repo access** (recommended): `loopy auth github`, then install the App on
   the repo in `sandboxes.BaseSandbox.repos`. (Or drop a `GITHUB_TOKEN` PAT in `secrets/base.env`.)
3. **Webhook secret** — generate one and put it in the control-plane env so the ingress can
   verify deliveries:
   ```
   echo "GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)" >> loopy.env
   ```
4. **Point GitHub at the endpoint.** In the App's (or repo's) webhook settings:
   - Payload URL: `<public_url>/hooks/github`
   - Content type: `application/json`
   - Secret: the value from step 3
   - Events: **Pull requests** (subscribe to Issues / Pushes too if you trigger on those)
5. **Model + sandbox creds:** `cp base.env.example secrets/base.env` and fill it in.

## Run it

```
loopy compile examples/github --out manifest.json
loopy run --in-process manifest.json
```

Open a PR (or merge one) on the target repo and watch the matching workflow fire. Inspect runs
with `loopy admin` (dashboard at http://127.0.0.1:9000).

### Try it without GitHub

Drive a built-in event by hand with a sample payload (the signature check applies only to the
HTTP edge, so a direct trigger skips it):

```
loopy trigger --event Github.PullRequestOpened \
  --fields '{"number":1,"repo":"octocat/Hello-World","title":"Add widget","branch":"feat/widget","base":"main","url":"https://github.com/octocat/Hello-World/pull/1"}'
```

> Heads up: if `GITHUB_WEBHOOK_SECRET` is unset, the `/hooks/github` endpoint runs **unverified**
> and the runner warns on startup. Set it before exposing the endpoint to the internet.
