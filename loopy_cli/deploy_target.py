"""Deploy targets: the named places the engine can run, chosen explicitly per command.

The engine itself is provider-agnostic (the serve/auth contract in
`docs/design/aws-deploy.md` and `docs/design/admin-auth.md`); a *deploy target* is the
CLI-side name for one way of hosting it, so a command like `loopy deploy <target>` can be
explicit about which deployment it stands up. (`loopy admin` no longer names a target: it
auto-routes from `LOOPY_PUBLIC_URL` — CloudFront → SSM tunnel, any other URL → proxy to
`/admin`, unset → the local run-state DB — with `--local`/`--tunnel`/`--url` as overrides.)

- `local` — no deployment: `loopy run` on this machine, read back by `loopy admin --local`
  (or bare `loopy admin` when no public URL is set). Never recorded in `loopy.env` (it
  isn't a hosting choice).
- `byo` — bring-your-own hosting: a platform (Render, Fly, Railway), a VPS, or a dev
  tunnel. You bring the public URL (`LOOPY_PUBLIC_URL`); `loopy init` prompts for it.
- `bootstrap` — the loopy-provisioned starter stack: `loopy deploy bootstrap` stands up
  the host from your cloud credentials and mints the URL. It happens to be implemented
  on AWS (EC2 + CloudFront) today, but the target is named for what it is — the
  batteries-included bootstrap — so future custom targets can claim provider names
  (`aws`, `render`, …) without colliding with it.

The hosting choice is recorded as `LOOPY_DEPLOY_TARGET` in `loopy.env` — by `loopy init`
(the wizard's hosting question) or by `loopy deploy bootstrap` itself. It's a CLI-side
hint only: it phrases `init`/`webhooks` guidance for the right path and lets `loopy admin`
point at the right next step in its "no local DB yet" error. The engine never reads it.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

DEPLOY_TARGET_ENV = "LOOPY_DEPLOY_TARGET"

TARGET_BYO = "byo"
TARGET_BOOTSTRAP = "bootstrap"

# What `loopy init` / `loopy deploy` record in loopy.env: a hosting choice (`local` isn't one
# — it's the no-deployment case, and `loopy admin` reaches it by the absence of a public URL).
HOSTED_TARGETS = (TARGET_BYO, TARGET_BOOTSTRAP)

# Written by `loopy deploy bootstrap` alongside LOOPY_PUBLIC_URL, so `loopy admin
# bootstrap` can print a ready-to-run SSM tunnel command and derive the tunneled
# dashboard URL. CLI-side hints like LOOPY_DEPLOY_TARGET — the engine never reads them.
BOOTSTRAP_INSTANCE_ID_ENV = "LOOPY_BOOTSTRAP_INSTANCE_ID"
BOOTSTRAP_ENGINE_PORT_ENV = "LOOPY_BOOTSTRAP_ENGINE_PORT"


def resolve_deploy_target(root: str | Path) -> str | None:
    """The recorded deploy target: process env > `loopy.env` > None.

    Same precedence every control-plane var gets (the real environment wins over the
    local dotenv). Returns None when unset or unrecognized, so callers fall back to
    target-agnostic behavior rather than trusting a stray value.
    """
    from loopy_runtime.secrets import load_control_plane_env

    value = os.environ.get(DEPLOY_TARGET_ENV, "").strip()
    if not value:
        value = load_control_plane_env(root).get(DEPLOY_TARGET_ENV, "").strip()
    value = value.lower()
    return value if value in HOSTED_TARGETS else None


def resolve_bootstrap_config(root: str | Path) -> dict[str, str]:
    """The bootstrap deploy's recorded client-side hints (instance id, engine port).

    Read with the usual precedence (process env > `loopy.env`); absent keys are simply
    missing from the dict, so callers fall back to placeholders/defaults.
    """
    from loopy_runtime.secrets import load_control_plane_env

    file_env = load_control_plane_env(root)
    out: dict[str, str] = {}
    for key in (BOOTSTRAP_INSTANCE_ID_ENV, BOOTSTRAP_ENGINE_PORT_ENV):
        value = os.environ.get(key, "").strip() or file_env.get(key, "").strip()
        if value:
            out[key] = value
    return out


def is_cloudfront_url(url: str | None) -> bool:
    """True if `url` points at a CloudFront distribution (host `*.cloudfront.net`).

    That's the public URL `loopy deploy bootstrap` mints, and the one place `/admin` is
    blocked at the edge: the CloudFront→origin hop is plain HTTP, so the bearer token must
    not travel it. `loopy admin` reads this signal to reach such a deploy over its SSM
    tunnel instead of proxying to `$LOOPY_PUBLIC_URL/admin`.
    """
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(".cloudfront.net")
