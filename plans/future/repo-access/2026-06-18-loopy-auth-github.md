# `loopy auth github` — one-command GitHub App onboarding (manifest flow)

**Status:** draft
**Owner:** peter
**Date:** 2026-06-18

## Goal
Give a self-hosting user a near-one-click way to create **their own** GitHub App and land its
credentials locally, so loopy can later mint short-lived, repo-scoped tokens for agents — with
**no loopy-owned central app and no persistent loopy server**. Optimizing for onboarding
simplicity above all.

## Context
- Agents run inside a sandbox and need authenticated git access to operate on a codebase (clone,
  push, open PRs). The sandbox is the trust boundary; secrets are injected into it at run time.
- We chose the **bring-your-own GitHub App** model: each deployment registers its own App. Minting
  installation tokens is **pure client-side** (private key → JWT → installation token via
  `api.github.com`); a server is only needed for *webhooks* / *OAuth user-login*, neither of which
  this uses. So loopy stays serverless and OSS-clean.
- Why App over PAT (decided earlier in this thread): the long-lived powerful secret (the App
  private key) **stays at the control-plane and never enters the sandbox**; only ephemeral,
  repo-scoped installation tokens cross into the agent. That's *more* secure than injecting a PAT,
  while remaining self-hosted.
- The only cost of the App model is registration friction. **GitHub's App Manifest flow** removes
  it: we POST a manifest, GitHub walks the user through creation, and hands back the app id +
  private key via a temporary code we exchange. ~2 clicks, creds written for the user.
- Existing surfaces we build on:
  - Control-plane secret home already exists: `loopy.env` + `load_control_plane_env()` merged into
    the process env with `setdefault` (`loopy_runtime/secrets.py`, `loopy_cli/__init__.py:145`).
    `loopy.env` is already gitignored (`.gitignore:20`).
  - CLI is a Typer app with lazy per-command imports (`loopy_cli/__init__.py`).

## Constraints & non-goals
**Constraints**
- **No persistent server.** The manifest-flow callback is a one-shot `127.0.0.1` listener that
  binds only for the redirect and is torn down immediately (the `gh auth login` pattern). "No
  server" = no hosted service, not "never bind a localhost port during setup."
- **Loopy owns nothing on GitHub.** The App is created under the *user's* account/org. No central
  app, no shared secret.
- **The App private key never goes near a sandbox.** It lives at the control-plane only; the
  runtime mints scoped tokens from it (next milestone).
- New third-party deps kept minimal (one: a JWT/RS256 lib). Prefer stdlib for browser + callback.
- Reuse the existing `loopy.env` control-plane surface; do not invent a new secret file.

**Non-goals (explicitly deferred — do NOT build here)**
- The runtime `TokenProvider` that mints a token and **injects it into the sandbox** + the git
  credential-helper wiring. *(Next milestone in this epic.)*
- The `repos:` sandbox registry field + the `network:`-membership compile check. *(Sibling
  milestone; this command works without it.)*
- The zero-config credential **discovery chain** (`gh`/git/env) default provider. *(Sibling
  milestone; complementary "instant dev start" path.)*
- PAT support, multi-App selection, non-GitHub SCMs.

## Approach
Add a `loopy auth` sub-command group with `loopy auth github`. It runs the GitHub App Manifest
flow end to end:

1. **Build a manifest** — name, `default_permissions` (`contents: write`, `pull_requests: write`,
   `metadata: read`), `public: false`, no webhook (`hook_attributes.active: false`), and a
   `redirect_url` pointing at the local callback.
2. **Serve a one-shot callback** on `127.0.0.1:<port>` (stdlib `http.server` in a thread).
3. **Open the browser** to a tiny locally-served HTML page that auto-submits a form POSTing the
   manifest to GitHub's create-app URL (personal: `https://github.com/settings/apps/new`; org:
   `https://github.com/organizations/<org>/settings/apps/new`). GitHub shows the create
   confirmation; on submit it redirects back to our callback with a temporary `?code=`.
4. **Exchange the code** — `POST https://api.github.com/app-manifests/{code}/conversions` →
   `{ id, pem, client_id, ... }`.
5. **Persist creds locally:**
   - Write the PEM to a gitignored file `./.loopy/github-app.pem` (mode `0600`). A multi-line PEM
     does not fit the simple `KEY=value` dotenv parser, so we store it as a file and reference it
     by path — cleaner and standard.
   - Merge `GITHUB_APP_ID=<id>` and `GITHUB_APP_PRIVATE_KEY_FILE=.loopy/github-app.pem` into
     `loopy.env` (create/append, never clobber unrelated keys).
   - Ensure `.loopy/` is gitignored.
6. **Guide installation** — the manifest flow *creates* the App but does not *install* it. Print
   (and optionally open) the install URL `https://github.com/apps/<slug>/installations/new` so the
   user picks which repos the App can touch.
7. **Verify (best-effort)** — if installed, mint a token via the new `scm/github_app` helper and
   report the installations/repos it can see, so the user gets a green check that creds work.

Credential mechanics (App JWT, token minting, installation discovery) live in a new
`loopy_runtime/scm/github_app.py` — pure-ish, reused by the runtime `TokenProvider` next milestone.
The browser/callback/manifest orchestration is onboarding-only and lives in the CLI layer.

### Alternatives considered
- **PAT in `loopy.env`** — simpler to read but worse onboarding (manual token creation) and worse
  security (broad long-lived secret enters the sandbox). Kept only as a future override.
- **Base64 the PEM into `loopy.env`** — avoids a second file but produces an opaque giant env
  value and complicates rotation/inspection. File-by-reference is cleaner.
- **Reuse FastAPI/uvicorn for the callback** — heavier than a stdlib one-shot handler for a single
  redirect; uvicorn is already a runtime dep but spinning it up for one request is overkill.

## Steps
- [x] `loopy_runtime/scm/__init__.py` + `loopy_runtime/scm/github_app.py`:
      `AppCredentials` (app_id, private_key_pem); `app_jwt()` (RS256, ~9-min exp);
      `find_installation(repo)` (`GET /repos/{owner}/{repo}/installation`);
      `mint_installation_token(installation_id, repos=None, permissions=None)`
      (`POST /app/installations/{id}/access_tokens`, scoped). Thin GitHub API client (stdlib
      `urllib` or `httpx` if already transitive).
- [x] `loopy_runtime/secrets.py`: `write_control_plane_env(root, updates: Mapping[str,str])` —
      merge-write keys into `loopy.env` preserving existing lines/comments; never log values.
- [x] `loopy_cli/auth.py`: the manifest-flow orchestration — `build_manifest()`,
      one-shot callback server, auto-submit HTML page, browser open, code→conversion exchange,
      PEM-file + `loopy.env` write, `.gitignore` ensure, install-URL guidance, verify.
- [x] `loopy_cli/__init__.py`: register an `auth` Typer group; add `loopy auth github`
      (`--org`, `--name`, `--port`, `--force`) and `loopy auth status` (show + verify stored creds).
      Lazy imports, matching existing commands.
- [x] `.gitignore`: add `.loopy/` (PEM + future local state).
- [x] `pyproject.toml`: add the JWT/RS256 dep (`pyjwt[crypto]`), pinned; keep everything else.
- [x] Tests: manifest shape; `write_control_plane_env` merge/idempotency/no-clobber; conversion
      exchange + cred write against a faked GitHub endpoint; JWT claims; token-mint request shape
      (scoped to repos) with a stubbed API. No real network in tests.
- [x] Docs: a short `DEPLOYMENT.md` section on `loopy auth github` (the onboarding path) +
      note the deferred runtime injection milestone.

## Files likely to change
- `loopy_runtime/scm/github_app.py` *(new)* — App JWT + installation-token minting + discovery.
- `loopy_runtime/scm/__init__.py` *(new)*.
- `loopy_cli/auth.py` *(new)* — manifest-flow onboarding orchestration.
- `loopy_cli/__init__.py` — register the `auth` group.
- `loopy_runtime/secrets.py` — `write_control_plane_env` writer.
- `.gitignore` — `.loopy/`.
- `pyproject.toml` — `pyjwt[crypto]`.
- `tests/...` — unit tests for the above (no live network).
- `DEPLOYMENT.md` — onboarding section.

## Open questions
- **Org vs personal:** auto-detect via a prompt, or require `--org <name>`? (Leaning: optional
  `--org`, default personal account.)
- **Default App permissions:** `contents: write` + `pull_requests: write` + `metadata: read` is the
  fix/PR baseline — confirm that's the right minimal default (a read-only loop would want less, but
  per-permission scoping is the deferred "complex permissioning").
- **HTTP client:** stdlib `urllib` (zero new dep) vs `httpx` (nicer, only if already transitive).
  Leaning stdlib for the API calls to avoid a dep.
- **`loopy auth status` scope:** just presence + a mint-test, or also list which `repos` the
  install covers? (Leaning: presence + mint-test + installed-repo count.)

## Notes / decisions
- **Built** (status: draft → implemented). Resolved the open questions as follows:
  - *Org vs personal:* optional `--org`, default personal account (no prompt).
  - *Default permissions:* shipped the `contents:write` + `pull_requests:write` + `metadata:read`
    fix/PR baseline; per-permission scoping stays the deferred "complex permissioning."
  - *HTTP client:* stdlib `urllib` for all GitHub API calls — zero new HTTP dep. The single network
    boundary is `github_app._request_json`, which tests stub.
  - *`loopy auth status`:* presence + mint-test + per-installation reachable-repo count.
- `app_jwt` uses a 9-min exp with `iat` backdated 60s for clock skew (GitHub caps JWTs at 10 min).
- `_SUCCESS_PAGE` keeps `.encode()` (non-ASCII ✓/→) while `_ERROR_PAGE` is a bytes literal — ruff
  UP012 only rewrites pure-ASCII.
- One new dep: `pyjwt[crypto]>=2.8`, imported lazily inside `app_jwt` so a bare
  `import loopy_runtime.scm.github_app` stays cheap.
- Added `loopy auth status`; the `--name`/`--port`/`--force`/`--no-browser` flags landed as planned.
- Tests in `tests/test_auth_github.py` (13) cover manifest shape, the env writer
  (merge/idempotency/no-clobber), JWT claims, conversion+cred-write, and scoped/unscoped token mint;
  no live network. Full suite green (239 passed, 1 pre-existing skip).
- **Follow-up milestone — runtime token injection — also built** (was a non-goal of this plan):
  - `loopy_runtime/contract.py`: new `TokenProvider` Protocol (`token_env(spec)`), merged into the
    sandbox env after `SecretsResolver` so minted creds win on conflict.
  - `loopy_runtime/scm/token_provider.py`: `GitHubAppTokenProvider` mints a scoped installation
    token (resolves the sole installation, or an explicit id), caches it until ~2 min before expiry,
    and returns `git_credential_env()` — `GITHUB_TOKEN` plus a per-host git credential helper via
    `GIT_CONFIG_*` (token never lands in `git config` output).
  - `loopy_runtime/runtime/inmemory.py`: optional `tokens=` param; `None` preserves prior behavior.
  - `loopy_cli/__init__.py`: `loopy run` builds the provider when a GitHub App is configured.
  - Tests in `tests/test_token_injection.py` (11): git wiring, mint/scope, caching + refresh,
    install resolution, and the runtime seam reaching the sandbox. Full suite: 250 passed, 1 skipped.
  - The per-sandbox `repos:` scoping field + `network:`-membership compile check remain deferred;
    today the token is scoped to the installation (the repos chosen at install time).
