"""The deployment mode chosen at `loopy init`: who provides the engine's public URL.

The public webhook URL is the one piece of setup that can't follow a single linear order,
because a provisioned host doesn't mint its URL until deploy time. So `loopy init` asks up
front which shape you're in and records it as `LOOPY_DEPLOY_MODE` in `loopy.env`:

- `byo` — you bring the URL: a domain, a dev tunnel (ngrok/cloudflared), or a platform
  hostname like Render's. `loopy init` prompts for `LOOPY_PUBLIC_URL`, so webhook
  registration is runnable right after init.
- `provisioned` — a `loopy deploy` mints the URL. `loopy init` skips the URL prompt;
  `loopy deploy aws` writes `LOOPY_PUBLIC_URL` back to `loopy.env` once the CloudFront
  distribution exists, and only then is `loopy webhooks github` runnable.

This is a CLI-side onboarding hint only — it seeds `init`'s flow and the next-step pointers,
and lets `loopy webhooks github` phrase its "no public URL" error for the right path. The
engine never reads it, so the serve/auth contract stays provider-agnostic (see
`docs/design/aws-deploy.md` and `docs/design/admin-auth.md`).
"""

from __future__ import annotations

import os
from pathlib import Path

DEPLOY_MODE_ENV = "LOOPY_DEPLOY_MODE"
MODE_BYO = "byo"
MODE_PROVISIONED = "provisioned"
VALID_MODES = (MODE_BYO, MODE_PROVISIONED)


def resolve_deploy_mode(root: str | Path) -> str | None:
    """The recorded deployment mode: process env > `loopy.env` > None.

    Same precedence every control-plane var gets (the real environment wins over the
    local dotenv). Returns None when unset or unrecognized, so callers fall back to
    mode-agnostic behavior rather than trusting a stray value.
    """
    from loopy_runtime.secrets import load_control_plane_env

    value = os.environ.get(DEPLOY_MODE_ENV, "").strip()
    if not value:
        value = load_control_plane_env(root).get(DEPLOY_MODE_ENV, "").strip()
    value = value.lower()
    return value if value in VALID_MODES else None
