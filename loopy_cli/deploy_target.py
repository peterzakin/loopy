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
  on AWS (EC2 + CloudFront) today, but the target is named for what it is (the
  batteries-included bootstrap) so future custom targets can claim provider names
  (`aws`, `render`, …) without colliding with it.

The choice is not persisted: `loopy init` uses it in-memory to order onboarding (a
provisioned host mints its URL only at deploy time, so bring-your-own is asked for the
URL now while bootstrap is not), and that's the end of its life. Later commands read the
state that actually exists — `LOOPY_PUBLIC_URL` and the CloudFront signal below — rather
than a recorded hint, so there's no `loopy.env` key to keep in sync.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

# The two hosting choices `loopy init` offers (`local` isn't one — it's the no-deployment
# case). Used in-memory to order onboarding; never written to loopy.env.
TARGET_BYO = "byo"
TARGET_BOOTSTRAP = "bootstrap"

# Written by `loopy deploy bootstrap` alongside LOOPY_PUBLIC_URL, so `loopy admin` can print
# a ready-to-run SSM tunnel command and derive the tunneled dashboard URL. Client-side hints
# only — the engine never reads them.
BOOTSTRAP_INSTANCE_ID_ENV = "LOOPY_BOOTSTRAP_INSTANCE_ID"
BOOTSTRAP_ENGINE_PORT_ENV = "LOOPY_BOOTSTRAP_ENGINE_PORT"


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
