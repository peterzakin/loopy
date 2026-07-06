# `loopy deploy render` — design

One command that takes a loopy project from "compiles on my laptop" to "live on Render,
webhooks delivering, URL written back" — the Render sibling of `loopy deploy bootstrap`.
Provider knowledge stays CLI-side (the serve contract in `docs_md/deployment.md` is
untouched: no `if render:` in the engine); everything Render-specific lives in one new
module, `loopy_cli/render.py`.

## Decisions already made (with the user)

- **Code reaches Render by git-push** with a *guided* preflight (detect problems, print or
  offer the exact fix; never create GitHub repos implicitly). Render builds the project's
  `Dockerfile` from the connected repo; every later `git push` auto-redeploys.
- **Auth is API-key-first**: `RENDER_API_KEY` (process env, then `loopy.env`) drives all
  provisioning through `api.render.com/v1` via `httpx` (already a core dep — no new
  dependencies). The `render` CLI is optional garnish only: when installed, the summary
  prints a ready-to-run `render logs --tail <service-id>` line. No shelling out for
  provisioning.
- **Plan is chosen interactively** on a TTY (manifest-aware trade-offs shown); headless
  runs must pass `--plan` and fail loudly without it (never pick a paid plan — or a
  correctness-breaking free plan — silently).
- **In scope**: idempotent re-deploy, `--destroy`, automatic webhook registration after
  the URL is live (a deliberate, documented divergence from bootstrap's nudge-only
  convention), and `loopy init` naming render as a hosting choice.
- **Out of scope (v1)**: log tailing as a built-in feature, image-registry deploys,
  Render Key Value (Redis) provisioning, custom domains, `render.yaml` blueprints
  (dashboard-only to instantiate — the reason this command exists).

## Command surface

```
loopy deploy render [manifest] [--root .]
    --plan <free|starter|standard|...>  # required when stdin isn't a TTY
    --service-name <name>   # default loopy-<project-dir>; the idempotency key
    --region <region>       # default oregon
    --branch <branch>       # default: the repo's current branch
    --disk-gb N             # persistent disk for .loopy/state.db (paid plans only)
    --yes                   # accept safe fixups (generate Dockerfile, etc.) without prompting
    --destroy               # delete the Render service; clean no-op if none exists
```

Structure mirrors `bootstrap`: lazy imports in the command body (so `loopy compile` never
pays for them), `--destroy` symmetry, and one stable identifier for idempotency (Render:
service name on first run, recorded service id afterwards; bootstrap: stack name).

## First-run setup wizard (interactive, house style)

The first `loopy deploy render` on a project is a short wizard in exactly `loopy init`'s
visual language: two-space indented `typer.prompt`/`typer.confirm`, numbered choices,
green `✓` / red `✗` echoes after each answer, and never re-asking for anything already
recorded (re-runs skip straight to the preflight checklist).

```
loopy deploy render

  Render setup — one-time, recorded in loopy.env (gitignored)

  1. Render API key
     Mint one at https://dashboard.render.com/settings#api-keys (Account Settings → API Keys)
  API key: rnd_************
  ✓ key verified (workspace: Vivek's Workspace) — wrote RENDER_API_KEY to loopy.env

  2. Workspace
  ✓ using Vivek's Workspace (the only workspace on this key)
     # multiple workspaces → numbered choice, echoed the same way

  3. Service
  Service name [loopy-hubble]:
  Region — 1) oregon  2) frankfurt  3) ohio  4) singapore  5) virginia
  Choose 1-5 [1]:
  ✓ web service loopy-hubble in oregon, deploying branch main

  4. Plan
     1) free     — $0. Spins down after ~15 min idle: webhooks wake it (cold start
                   delay), but your project has cron workflows and free-tier spin-down
                   WILL miss their ticks. Run history is lost on restart (no disk).
     2) starter  — ~$7/mo. Always on (cron fires), can attach a persistent disk.
  Choose 1 or 2 [2]:
  ✓ starter plan
```

Wizard rules:

- **Every prompt has a flag/env equivalent** (`RENDER_API_KEY`, `--service-name`,
  `--region`, `--branch`, `--plan`) so agents and CI never see a prompt. On a non-TTY,
  a missing required answer exits 1 naming the exact flag to pass.
- **API key verification is immediate**: `GET /v1/owners` on entry. A bad key is a red
  `✗` and a re-prompt, not a failure 30 seconds later mid-provision. The same call
  resolves the `ownerId` every create needs, and lists workspaces for step 2.
- **The cron annotation in the plan prompt is computed, not boilerplate**: the compiled
  manifest is inspected for `cron(...)` triggers, and the free-plan line only carries the
  "WILL miss ticks" warning when the project actually has them.
- **What persists where**: `RENDER_API_KEY` → `loopy.env` at answer time
  (`write_control_plane_env`, the `DAYTONA_API_KEY` channel). Service name, region,
  branch, plan → live on the Render service itself; after creation the recorded
  `LOOPY_RENDER_SERVICE_ID` is the source of truth, so re-runs read the service and
  nothing drifts. The deploy-target choice is never persisted (same rule as
  `deploy_target.py` documents for init).

## Preflight — every check before any mutation

All checks run up front and render as one checklist (bootstrap's `_Progress` visual
style); nothing is created or modified on Render until the required checks pass. Each
failing line carries the exact remediation — a command to run, or an offer to fix it
in-place (accepted automatically under `--yes`).

| # | Check | On failure |
|---|---|---|
| 1 | Project compiles (`_resolve_manifest` + compile) | Fail with compile diagnostics — same gate as bootstrap. |
| 2 | `loopy.env` exists with control-plane creds (`DAYTONA_API_KEY` at minimum) | Fail: "run `loopy init`" — mirrors `bootstrap.py`'s preflight. |
| 3 | `LOOPY_ADMIN_TOKEN` present in `loopy.env` | Offer to mint one (CSPRNG, `loopy_sk_` prefix — init's generator) so `/admin` mounts on the deployed engine. Declining = warn: dashboard won't be reachable. |
| 4 | Every sandbox `env_file` + `sensors/.env` present on disk (`collect_secret_files`) | Fail naming each missing file — the engine errors at run time without them (`EnvFileSecretsResolver`), so catch it here. |
| 5 | Directory is a git repo | Fail: print `git init && git add -A && git commit` guidance. |
| 6 | A GitHub/GitLab remote exists | Fail: print `git remote add origin …` / `gh repo create --source . --push` as copy-paste suggestions (we never run repo creation ourselves). |
| 7 | Working tree clean | Warn + confirm on dirty (Render builds the *pushed* tree, so local edits won't deploy); `--yes` proceeds. |
| 8 | Branch pushed and up to date with the remote | Fail on never-pushed (`git push -u origin <branch>` printed); warn + confirm on unpushed commits. |
| 9 | `Dockerfile` present at root | Offer to generate via the existing `loopy dockerfile` logic (writes `Dockerfile` + `.dockerignore`), then remind: commit and push before continuing — Render builds from the remote, so an uncommitted Dockerfile fails check 7/8 on the re-check. |
| 10 | `Dockerfile` version pin matches installed loopy | Warn on drift; offer to regenerate (same as 9). |
| 11 | `RENDER_API_KEY` valid (`GET /v1/owners`) | Wizard step 1 (TTY) or exit-1 with the mint URL (headless). |
| 12 | Repo visible to Render | Can't be fully preflighted (no public API to enumerate connected repos); on a create failing with Render's "repo not found / not connected" error, translate to: "private repo — connect GitHub to Render once at https://dashboard.render.com → New → Web Service, then re-run." |

Checks 5–10 print as they resolve, so a healthy project's preflight reads as a fast
column of `✓`s and the first run on a fresh project reads as a guided to-do list.

## Provisioning flow

1. **Find-or-create.** Recorded `LOOPY_RENDER_SERVICE_ID` in `loopy.env` → `GET
   /v1/services/{id}` (deleted-out-of-band falls back gracefully). Otherwise find by
   name via `GET /v1/services?name=`. Found → update path; not → `POST /v1/services`
   with `type: web_service`, runtime `docker`, repo URL, branch, plan, region,
   `autoDeploy: yes`, and the env-var block inline.
2. **Env vars.** Computed by a helper extracted from today's `loopy env` command (shared,
   not duplicated): all `loopy.env` keys minus `_ENV_DEPLOY_SKIP`
   (`LOOPY_PUBLIC_URL`, `REDIS_URL`, `LOOPY_ADMIN_TOKEN_NEXT`), values parsed by the
   dotenv parser so quoted values arrive unquoted (regression-tested — this exact quoting
   bug bit a real deploy). Updates use `PUT /v1/services/{id}/env-vars` (replace-all →
   idempotent). `RENDER_API_KEY` itself is excluded — the engine never needs it.
3. **Secret files.** Each `collect_secret_files()` rel-path becomes a Render Secret File —
   except `loopy.env` itself: the env-var push above already carries the control plane,
   and the file holds `RENDER_API_KEY`, which the deployed service must never receive.
   Confirmed against current Render docs: secret-file names are flat and mounted at
   `/etc/secrets/<filename>`, Docker-runtime services see them **only** there (the
   repo-root copy applies to non-Docker services), and all files together are capped at
   1 MB (loopy's dotenvs are far under). So: the name is the rel-path with `/` encoded
   as `__` (`secrets/base.env` → `secrets__base.env`), and the generated Dockerfile's
   start command gains a small shim that links `/etc/secrets/<encoded-name>` to
   `/project/<rel-path>` before exec'ing `loopy run`, so `EnvFileSecretsResolver` finds
   every file at the path the manifest names, unchanged. The engine image runs as root
   (`python:3.12-slim` default), so Render's group-`1000` read permission on
   `/etc/secrets` is not an issue; if the image ever drops root, `usermod -a -G 1000`
   joins the Dockerfile. (Only the REST endpoint shape for writing secret files gets
   re-verified at implementation time.)
4. **Deploy + wait.** Trigger/observe the deploy, poll to `live` under a `_Progress`
   board (build → deploy → healthz rows), then reuse `wait_until_serving()` against
   `https://<slug>.onrender.com/healthz`. Failed builds fetch and print the deploy's
   failure via API when available, always with the dashboard deep-link to the build logs.
5. **Write-back.** `write_control_plane_env(root, {LOOPY_PUBLIC_URL, LOOPY_RENDER_SERVICE_ID})`.
   The service id joins `deploy_target.py` as a client-side hint (like
   `LOOPY_BOOTSTRAP_INSTANCE_ID`): fast idempotent lookup + `--destroy` target; the
   engine never reads it.
6. **Webhooks — registered automatically.** Call `sync_github_webhooks()` (the function
   behind `loopy webhooks github`) with the fresh URL. Non-fatal: the deploy already
   succeeded, so a webhook failure prints the error and the exact retry command instead
   of failing the run. The divergence from bootstrap's nudge-only convention is
   deliberate (this command's promise is "full setup end to end") and documented at the
   call site.
7. **Summary block** (bootstrap's format):

   ```
   deploy: done. Engine at https://loopy-hubble.onrender.com
     status:    live (/healthz is answering)
     plan:      starter (always on)          # free adds the spin-down caveat here
     url:       wrote LOOPY_PUBLIC_URL=… to loopy.env
     webhooks:  registered: github → …/hooks/github (2 events)
     dashboard: loopy admin   (proxies to /admin with your bearer token)
     logs:      render logs --tail srv-…     # only when the render CLI is installed
     cd:        git push origin main redeploys automatically
     teardown:  loopy deploy render --destroy
   ```

`--destroy`: recorded id (fallback find-by-name) → `DELETE /v1/services/{id}`, remove
`LOOPY_RENDER_SERVICE_ID` (and offer to clear `LOOPY_PUBLIC_URL` if it points at the
deleted service) from `loopy.env`. Nothing found → clean "nothing to delete" no-op.

## Error handling

- Every API failure prints Render's own error body plus exactly one next action
  (401 → mint-a-key URL; 402/plan errors → the plan flag; "repo not connected" →
  the one-time dashboard connect step).
- Ctrl-C anywhere is safe: all mutations are idempotent, so the recovery story is
  always "re-run the same command".
- `/healthz` never answering surfaces the deploy's log tail (API) or the dashboard
  deep-link — the bootstrap `_engine_diagnostics` role, Render-flavored.
- Non-TTY with missing inputs: exit 1 naming the flag, before any network call.

## Code layout

- **`loopy_cli/render.py` (new)** — `RenderClient` (thin httpx wrapper: auth header,
  JSON, pagination, readable errors), the wizard, the preflight checklist, the `render`
  command. Registered on the shared `deploy_app`.
- **`loopy_cli/deploy_cmd.py` (new, small)** — `deploy_app` moves here from
  `bootstrap.py` so no provider module owns the shared command group; `bootstrap.py`
  and `render.py` both register into it. (Targeted improvement, not a refactor spree.)
- **`loopy_cli/__init__.py`** — extract `loopy env`'s block computation into a shared
  helper used by both the command and the render target; `_DOCKERFILE_TEMPLATE` CMD
  gains the secret-file link shim (inert when `/etc/secrets` is absent, e.g. local
  docker).
- **`loopy_cli/deploy_target.py`** — `TARGET_RENDER`, `RENDER_SERVICE_ID_ENV`; docstring
  updated (render is now a real target, not a hypothetical).
- **`loopy init`** — hosting question gains option 3: "Deploy to Render — `loopy deploy
  render` sets LOOPY_PUBLIC_URL for you at deploy" (defers the URL prompt exactly like
  bootstrap); `_report_remaining_setup` orders next steps accordingly.
- **Docs** — `docs_md/deployment.md` gets a Render-target section (it already carries the
  provider table naming Render); `docs/design/render-deploy.md` records the full design.

## Testing

Matches the repo's existing test conventions around `bootstrap.py` (pure helpers unit
tested; no live cloud calls in CI):

- **Preflight matrix** — no repo / no remote / dirty tree / unpushed / missing env files /
  missing Dockerfile / stale pin, each asserting the check verdict and its remediation text.
- **Env block** — skip-list respected, quote-stripping regression test, `RENDER_API_KEY`
  never emitted.
- **Secret-file encoding** — rel-path ↔ flat-name round-trip, including nested dirs.
- **Create-vs-update decision** — recorded id, stale id (404 → find-by-name → create),
  name collision, against canned `httpx.MockTransport` responses.
- **Wizard gating** — TTY prompts vs non-TTY flag requirements (plan, API key), and
  never-re-ask on re-run.
- **Destroy** — recorded id, fallback by name, nothing-found no-op, env-key cleanup.
- **CLI wiring** — `loopy deploy render --help` renders; `deploy_app` relocation keeps
  `loopy deploy bootstrap` intact.

During implementation (not CI), one live end-to-end run against a real Render workspace
validates the recorded API fixtures, secret-file behavior, and the flat-name assumption.
