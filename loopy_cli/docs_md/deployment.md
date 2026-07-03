# Loopy — the deployment reference

How to run the control plane somewhere other than your laptop, and how to watch it safely.
For authoring a project, run `loopy docs`; for the diagnostic-code catalog, `loopy docs
errors`. The full design (including the threat model and the OIDC upgrade path) lives in
`docs/design/admin-auth.md` in the repo.

## The provider-agnostic serve contract

Loopy depends only on the lowest common denominator every hosting provider exposes. There
are no provider SDKs and no `if render:` branches in the serve path; everything else is
per-provider configuration, not loopy code.

- **A process env var** — secrets arrive through the environment (`LOOPY_ADMIN_TOKEN`,
  `REDIS_URL`, `DAYTONA_API_KEY`). For local dev, `loopy.env` at the project root feeds the
  same variables; the real environment always wins (the dotenv is merged with `setdefault`).
- **A TCP port** — `loopy admin` honors a platform-injected `$PORT`, falling back to
  `--port`, then 9000.
- **An HTTP ingress that terminates TLS** — loopy never terminates TLS itself; the platform
  (or nginx/caddy on a VM) does.
- **Durable storage behind the `StateStore` protocol** — SQLite on a persistent disk today;
  a networked store (Postgres/Redis) is an additive swap behind the same protocol.

| Concern | loopy depends on | Render | Fly / Railway | k8s | Bare VM |
|---|---|---|---|---|---|
| Secret | `LOOPY_ADMIN_TOKEN` from env | dashboard env var | `fly secrets` / env | Secret → env | env / `loopy.env` |
| TLS | terminated by the ingress | auto on `*.onrender.com` | auto | Ingress + cert-manager | nginx/caddy |
| Port | `$PORT` (fallback `--port`) | injected | injected | containerPort | flag/env |
| Persistence | `StateStore` (SQLite file) | persistent disk | volume | PVC | disk |

## The admin dashboard's three modes

`loopy admin` serves the read-only dashboard. Which mode you get follows from the bind and
flags — there is no separate "secure" binary:

1. **Local (default).** Binds loopback, no auth, reads `.loopy/state.db` directly. Unchanged
   from the dev loop: `loopy run` in one terminal, `loopy admin` in another.
2. **Exposed server** (`--host 0.0.0.0`, on the control plane). Requires
   `LOOPY_ADMIN_TOKEN` in the environment; every `/api/*` route then demands
   `Authorization: Bearer <token>`, verified in constant time before any handler runs.
   Run it co-located with `loopy run` against the same durable state DB (SQLite is one
   writer plus readers on one host). `GET /healthz` stays open — liveness only, no data —
   for platform probes.
3. **Remote client** (`--remote <url>`, on your laptop). A local loopback proxy that serves
   the dashboard UI and forwards `/api/*` (and `/static`) to the control plane, injecting
   the bearer from `LOOPY_ADMIN_TOKEN` (environment or `loopy.env`). The browser never
   holds the credential. Mutually exclusive with the local DB argument.

```bash
# mint one token, set it in two places
python -c "import secrets; print('loopy_sk_' + secrets.token_urlsafe(32))"
#   control plane: LOOPY_ADMIN_TOKEN in the platform env
#   laptop:        LOOPY_ADMIN_TOKEN in loopy.env (gitignored, never deployed)

loopy admin --host 0.0.0.0                        # control plane; honors $PORT
loopy admin --remote https://loopy.example.com    # laptop
```

## The auth mechanism and its guardrails

A single high-entropy shared bearer token: 256 bits from the CSPRNG, prefixed `loopy_sk_`
so secret scanners can match it. Presented as `Authorization: Bearer <token>` over TLS —
never a query param (leaks into access logs), never a cookie (drags in CSRF). The wire
format is identical to a future OIDC/JWT upgrade, so this is not a dead end.

The non-negotiable guardrails:

1. **Constant-time compare** — `secrets.compare_digest`, never `==` (a naive compare is a
   timing oracle).
2. **High entropy** — CSPRNG, 256 bits.
3. **TLS only** — the client refuses to send the token over plain HTTP to a non-loopback
   host.
4. **Read-only scope** — the dashboard has no write routes, valid token or not.
5. **Fail-closed** — a non-loopback bind with no token configured refuses to start. Run and
   step outputs are not redacted, so this is the invariant that prevents ever serving them
   openly.

**Rotation.** The server accepts two tokens during an overlap window: set
`LOOPY_ADMIN_TOKEN_NEXT` alongside `LOOPY_ADMIN_TOKEN`, roll the laptop and the platform
env, then drop the old value. No hard cutover, no mid-session lockout.

**Accepted tradeoff.** The token is symmetric (no hash-at-rest): a control-plane
secret-store compromise yields a live credential. Accepted deliberately — that store
already holds higher-value secrets (`ANTHROPIC_API_KEY`, git tokens), the dashboard token
is read-only, and the leak window is bounded by rotation.
