# Design: authentication for `loopy admin` against a remote control plane

Status: proposed · Scope: `loopy_runtime/dashboard`, `loopy_cli`, `loopy_runtime/secrets.py`

## Problem

`loopy admin` today is a read-only viewer that binds `127.0.0.1:9000` and reads the
run-state DB (`.loopy/state.db`) directly (`loopy_cli/__init__.py`, `admin`). Its only
protection is that it listens on loopback. We want to run the dashboard **locally** but
point it at a **live, cloud-hosted control plane** so an operator can watch production runs
from their laptop. The moment the run-state read path leaves the box it needs
authentication, and the auth must be standard practice with strong guarantees while staying
**agnostic to the hosting provider** (Render, Fly, Railway, k8s, a bare VM, …).

## What the dashboard exposes (why auth is mandatory here)

The registry view already redacts secret *values* (`loopy_runtime/dashboard/views.py`:
`_redact_sandbox`, "secrets: redacted"). But **run outputs, step outputs, and event
payloads are not redacted** — they carry diffs, root-cause text, links, and business data.
So exposing `/api/*` to the network is only safe behind an auth check that runs *before* any
handler.

## Decisions (the agreed design)

1. **Split: local UI, remote authenticated data.** The dashboard renders locally; the data
   is pulled from the remote control plane over HTTPS. The operator's browser never holds a
   credential.
2. **Client is a backend-for-frontend proxy, not a `RemoteStateStore`.** The `/api/*`
   surface is view-shaped (built by `build_run_detail` / `build_workflows` /
   `build_sensors`), not raw-store-shaped, so `loopy admin --remote <url>` runs a small local
   process that serves the static UI and **proxies** `/api/*` + `/static` to `<url>`,
   injecting the bearer header. This holds the token in a local process (from `loopy.env`),
   keeps it out of the browser (no XSS exposure), and avoids duplicating the view logic.
3. **Auth = symmetric shared bearer secret.** A single high-entropy token, presented as
   `Authorization: Bearer <token>` over TLS. The control plane compares it in constant time.
   Chosen deliberately: the asymmetry benefit of hash-at-rest is marginal here because the
   control plane's secret store already holds higher-value secrets (`ANTHROPIC_API_KEY`, git
   tokens); if that store leaks, a read-only dashboard token is the least of the problems.
   The wire format (`Authorization: Bearer`) is identical to the future OIDC upgrade, so this
   choice is not a dead end.
4. **Provider-agnostic via 12-factor.** The secret is read from the **process environment**
   (`LOOPY_ADMIN_TOKEN`); TLS is terminated by the platform ingress, not by loopy; durable
   run-state lives behind the `StateStore` Protocol. loopy depends only on the lowest common
   denominator every provider exposes and never imports a provider SDK or branches on the
   provider.

## Topology

```
browser ──▶ localhost:9000  (local `loopy admin --remote` process)
                 │  reads LOOPY_ADMIN_TOKEN from loopy.env, injects it
                 ▼
            proxy ──HTTPS, Authorization: Bearer──▶ control plane /api/*
                                                     (edge auth: constant-time compare,
                                                      then existing read-only handlers,
                                                      reading a durable StateStore)
```

| Host | Role | Secret source |
|---|---|---|
| Laptop | admin client (proxy + UI) | `loopy.env` → `LOOPY_ADMIN_TOKEN` (local-dev convenience) |
| Control plane | server (`/api/*` behind auth) | `LOOPY_ADMIN_TOKEN` in the **platform environment** (production mechanism) |

The two sides never share a file — `loopy.env` is gitignored and is not part of any deploy
artifact. `load_control_plane_env` (`loopy_runtime/secrets.py`) already merges the dotenv with
`setdefault`, so the platform environment always wins; that is the provider-agnostic contract.
Same value, delivered the right way per host.

## Provider-agnostic contract

loopy depends only on: a **process env var**, a **TCP port** (honor `$PORT`, fall back to
`--port`), an **HTTP ingress that terminates TLS**, and **durable storage behind
`StateStore`**. Everything else is per-provider config, not loopy code.

| Concern | loopy depends on | Render | Fly / Railway | k8s | Bare VM |
|---|---|---|---|---|---|
| Secret | `LOOPY_ADMIN_TOKEN` from env | dashboard env var | `fly secrets` / env | Secret → env | env / `loopy.env` |
| TLS | requires it, binds behind ingress | auto on `*.onrender.com` | auto | Ingress + cert-manager | nginx/caddy |
| Port | `$PORT` (fallback `--port`) | injects `$PORT` | injects `$PORT` | containerPort | flag/env |
| Persistence | `StateStore` Protocol | persistent disk or managed PG | volume or PG | PVC or PG | disk or PG |

Two rules keep it agnostic:

- **No provider SDKs, no `if render:` branches** in the auth or serve path. Config comes in
  through env + flags; loopy stays ignorant of who's hosting it.
- **Extend through the existing Protocols.** Secret sourcing beyond a bare env var (Vault /
  AWS Secrets Manager / GCP Secret Manager) is an additive resolver in the shape of the
  existing `SecretsResolver` / `TokenProvider` Protocols (`loopy_runtime/contract.py`);
  durable state beyond SQLite is a `StateStore` swap (the `sqlite.py` header already reserves
  "networked store, Redis/Postgres, behind the same Protocol"). Neither is built now; neither
  is foreclosed.

## Auth mechanism, in detail

- **Issuance.** The token is a high-entropy random string, `secrets.token_urlsafe(32)`
  (256 bits), with a `loopy_sk_` prefix so secret scanners can match it. Set once in the
  control plane's platform env and once in the operator's `loopy.env`.
- **Transmission.** `Authorization: Bearer <token>`, TLS only. Never a query param (leaks
  into access logs), never a cookie (drags in CSRF).
- **Verification (edge).** A FastAPI dependency wraps the whole `/api/*` router and runs
  before any handler: read the bearer, `secrets.compare_digest` against the configured
  token(s), 401 on missing/invalid. Read-only scope is inherent — the app has no write
  routes.
- **Rotation.** The server accepts two tokens during an overlap window (`LOOPY_ADMIN_TOKEN`
  + `LOOPY_ADMIN_TOKEN_NEXT`); roll the laptop and the platform env, then drop the old one.
  No hard cutover, no mid-session lockout.

### Five guardrails (non-negotiable)

1. **Constant-time compare** — `secrets.compare_digest`, never `==` (a naive compare is a
   timing oracle that leaks the secret byte by byte). This is the single most important line.
2. **High entropy** — CSPRNG, 256 bits.
3. **TLS only** — platform-terminated; loopy refuses to treat plain HTTP as authenticated
   transport for a non-loopback bind.
4. **Read-only scope** — the dashboard exposes nothing that mutates state, valid token or not.
5. **Fail-closed** — a non-loopback bind with no token configured **refuses to start** with
   an actionable error. This is the invariant that prevents ever serving unredacted run data
   openly.

## Implementation plan

### Phase 0 — Config & conventions (no behavior change)
- Register `LOOPY_ADMIN_TOKEN` (+ `LOOPY_ADMIN_TOKEN_NEXT`) as recognized control-plane env
  keys; document them in `loopy_runtime/secrets.py` alongside the existing control-plane
  creds note.
- Add validation/help in `loopy_runtime/config.py` if the serve path grows config keys.

### Phase 1 — Server-side edge auth
- New `loopy_runtime/dashboard/auth.py`: `AdminAuth` loads token(s) from env; `require_admin`
  is a FastAPI dependency doing the constant-time compare and 401.
- `create_app(store, manifest, *, auth=None)` gains the optional `auth`; when present, apply
  `require_admin` to every `/api/*` route. Add an unauthenticated `/healthz` (liveness only,
  no data) for platform probes.
- Enforce **fail-closed**: the serve entry refuses a non-loopback bind without a configured
  token.
- Honor `$PORT` when no `--port` is given.
- Tests: 401 (missing/invalid), 200 (valid), rotation (both tokens valid), `/healthz` open,
  fail-closed on non-loopback without token, no token value in logs.

### Phase 2 — Serving the API from the control plane
- The dashboard app runs in two modes:
  - **local** (today): loopback, no auth, reads SQLite directly — unchanged.
  - **remote-exposed**: bound to `$PORT`/`0.0.0.0`, auth required, reading the same **durable**
    `StateStore` the engine writes.
- ~~Decision to confirm~~ **Decided:** the engine itself mounts the dashboard at `/admin` on
  its webhook server, path-routed off the one public URL — deliveries at
  `$LOOPY_PUBLIC_URL/hooks/*`, dashboard at `$LOOPY_PUBLIC_URL/admin`. One process, one
  port, one hostname, so the admin endpoint is deterministic on every provider (Render
  exposes a single port per service; no reverse proxy or second service needed) and the
  engine's own store object serves the views. Mount policy mirrors `loopy admin`: open on a
  loopback bind; behind `LOOPY_ADMIN_TOKEN` on a non-loopback bind; **not mounted at all**
  on a non-loopback bind without a token (fail-closed by absence — webhooks still serve).
  The standalone hardened `loopy admin --host 0.0.0.0` remains for split deployments.
  Multi-instance deployments need a networked `StateStore` (Postgres) — a separate
  workstream, out of scope here.

### Phase 3 — Client-side remote mode (BFF proxy)
- `loopy admin --remote [url]`: start a local loopback server that serves the static UI and
  proxies `/api/*` + `/static` to the remote with `Authorization: Bearer` from
  `LOOPY_ADMIN_TOKEN`. Bare `--remote` derives the URL deterministically as
  `$LOOPY_PUBLIC_URL/admin` (see the Phase 2 decision); an explicit URL wins. In remote mode
  the positional argument is the URL, not a local `db` path.
- Clean errors: 401 → "auth failed, check LOOPY_ADMIN_TOKEN"; connection/TLS errors →
  actionable; refuse to start if the token env var is unset.
- Tests: header injection, 401 surfaced as an actionable message, refusal without token.

### Phase 4 — Docs
- Update the README "Watching runs" section and add a deployment note: the env-var contract,
  TLS-at-ingress, durable storage per provider, the agnostic table, and the fail-closed rule.
- Mirror into `loopy docs` deployment content. (The `loopy-landing` docs deployment page is a
  follow-up in that repo.)

## Security guarantees and accepted tradeoffs

- **Guarantees:** credential never in git, never in the manifest, never in the browser;
  transport is TLS-only; comparison is constant-time; read-only scope; fail-closed on
  misconfiguration; revocation and rotation without downtime.
- **Accepted tradeoff (symmetric):** a control-plane secret-store compromise yields a live
  credential (no hash-at-rest). Accepted because that store already holds higher-value
  secrets, the token is read-only, and the leak window is bounded by rotation.
- **Open question — payload redaction:** run/step outputs and event payloads are not
  redacted. Default for v1: auth-gated full payloads (holders are trusted operators). A
  redacted projection is a possible future option, behind the same endpoints.

## Non-goals (deliberately deferred)

- Per-user identity, OIDC/SSO, MFA. The upgrade path is clean: swap `AdminAuth`'s compare for
  JWT validation (signature/`iss`/`aud`/`exp`) via an OAuth device-code login; the wire format
  (`Authorization: Bearer`), the proxy, and the edge dependency shape do not change.
- A `PostgresStateStore` (multi-instance durability) — separate workstream behind the
  existing `StateStore` Protocol.
- A secret-manager resolver — additive later behind a `SecretsResolver`-shaped interface.
