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

1. **Public URL** — put the base URL where this loopy server is reachable in the control-plane
   env (or answer the hosted-URL prompt in `loopy init`, which writes the same key):
   ```
   echo "LOOPY_PUBLIC_URL=https://<your-host>" >> loopy.env
   ```
   (For local dev, expose `http://127.0.0.1:8000` with a tunnel, e.g. `cloudflared`/`ngrok`,
   and use the tunnel URL.)
2. **GitHub App** (repo access + webhook): `loopy auth github`, then install the App on the
   repo in `sandboxes.BaseSandbox.repos`. With `LOOPY_PUBLIC_URL` set, the App is created with
   its webhook already pointing at `<url>/hooks/github`, subscribed to pull requests / issues /
   issue comments / pushes, and the GitHub-minted `GITHUB_WEBHOOK_SECRET` lands in `loopy.env`
   — deliveries are signature-verified with no manual webhook setup.

   *Manual fallback* (App created before the URL existed, or a plain repo webhook): in the
   App's (or repo's) webhook settings set Payload URL `https://<your-host>/hooks/github`,
   content type `application/json`, a secret you generate (`openssl rand -hex 32`), and the
   events you trigger on — then put that secret in `loopy.env` as `GITHUB_WEBHOOK_SECRET`.
3. **Model + sandbox creds:** `cp base.env.example secrets/base.env` and fill it in.

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
