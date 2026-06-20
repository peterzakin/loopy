# github — react to GitHub webhooks

Two loops driven by GitHub deliveries to a single webhook URL:

| When | Sensor emits | Workflow | What the agent does |
| --- | --- | --- | --- |
| A PR is **opened** | `PullRequestOpened` | `workflows/review/code-review.md` | reviews the diff and posts review comments |
| A PR is **merged** | `PullRequestMerged` | `workflows/scan/find-work.md` | proposes the follow-on work the PR implies |

GitHub posts *every* event type to one URL, so both sensors listen on `/hooks/github`. The
runner verifies the delivery signature once at the edge, then fans it out to both sensors;
each returns its event only for the deliveries it cares about (a PR `opened` vs `closed`+merged).

## How it fits together

```
GitHub ──webhook(/hooks/github, X-Hub-Signature-256)──▶ loopy ingress
   verify HMAC  ─▶  fan out to both sensors  ─▶  on_pull_request_opened ─▶ PullRequestOpened ─▶ code-review
                                              └▶  on_pull_request_merged ─▶ PullRequestMerged ─▶ find-work
                         (each step runs in a sandbox with a scoped GitHub token injected)
```

- **Inbound trust:** the webhook secret verifies `X-Hub-Signature-256` (`scm/github_webhook.py`).
- **Outbound trust:** `loopy auth github` configures a GitHub App; the runtime mints a
  short-lived, repo-scoped installation token per step and injects it into the sandbox — no
  static PAT crosses the boundary (`scm/token_provider.py`).

## Setup

1. **GitHub App for repo access** (recommended): `loopy auth github`, then install the App on
   the repo in `sandboxes.Dev.repos`. (Or drop a `GITHUB_TOKEN` PAT in `secrets/dev.env`.)
2. **Webhook secret** — generate one and put it in the control-plane env so the ingress can
   verify deliveries:
   ```
   echo "GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)" >> loopy.env
   ```
3. **Point GitHub at the endpoint.** In the App's (or repo's) webhook settings:
   - Payload URL: `https://<your-host>/hooks/github`
   - Content type: `application/json`
   - Secret: the value from step 2
   - Events: **Pull requests**
   (For local dev, expose `http://127.0.0.1:8000` with a tunnel, e.g. `cloudflared`/`ngrok`.)
4. **Model + sandbox creds:** `cp dev.env.example secrets/dev.env` and fill it in.

## Run it

```
loopy compile examples/github --out manifest.json
loopy run --in-process manifest.json
```

Open a PR (or merge one) on the target repo and watch the matching workflow fire. Inspect runs
with `loopy admin` (dashboard at http://127.0.0.1:9000).

### Try it without GitHub

Drive a sensor by hand with a sample payload (signature check applies only to the HTTP edge):

```
loopy trigger --event PullRequestOpened \
  --fields '{"number":1,"repo":"octocat/Hello-World","title":"Add widget","branch":"feat/widget","base":"main","url":"https://github.com/octocat/Hello-World/pull/1"}'
```

> Heads up: if `GITHUB_WEBHOOK_SECRET` is unset, the `/hooks/github` endpoint runs **unverified**
> and the runner warns on startup. Set it before exposing the endpoint to the internet.
