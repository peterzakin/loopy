"""Deploy targets: the named places the engine can run, chosen explicitly per command.

The engine itself is provider-agnostic (the serve/auth contract in
`docs/design/aws-deploy.md` and `docs/design/admin-auth.md`); a *deploy target* is the
CLI-side name for one way of hosting it, so commands like `loopy deploy <target>` and
`loopy admin <target>` can be explicit about which deployment they act on:

- `local` — no deployment: `loopy run` on this machine. `loopy admin local` reads the
  run-state DB it writes. Never recorded in `loopy.env` (it isn't a hosting choice).
- `byo` — bring-your-own hosting: a platform (Render, Fly, Railway), a VPS, or a dev
  tunnel. You bring the public URL (`LOOPY_PUBLIC_URL`); `loopy init` prompts for it.
- `bootstrap` — the loopy-provisioned starter stack: `loopy deploy bootstrap` stands up
  the host from your cloud credentials and mints the URL. It happens to be implemented
  on AWS (EC2 + CloudFront) today, but the target is named for what it is — the
  batteries-included bootstrap — so future custom targets can claim provider names
  (`aws`, `render`, …) without colliding with it.

The hosting choice is recorded as `LOOPY_DEPLOY_TARGET` in `loopy.env` — by `loopy init`
(the wizard's hosting question) or by `loopy deploy bootstrap` itself. It's a CLI-side
hint only: it phrases `init`/`webhooks` guidance for the right path and lets `loopy
admin` point at the right target in its errors. The engine never reads it.

Older projects recorded the same fork as `LOOPY_DEPLOY_MODE` (`byo`/`provisioned`);
`resolve_deploy_target` still honors that spelling, mapping `provisioned` → `bootstrap`.
"""

from __future__ import annotations

import os
from pathlib import Path

DEPLOY_TARGET_ENV = "LOOPY_DEPLOY_TARGET"

TARGET_LOCAL = "local"
TARGET_BYO = "byo"
TARGET_BOOTSTRAP = "bootstrap"

# What `loopy init` / `loopy deploy` record in loopy.env (a hosting choice) vs. what
# `loopy admin` accepts (any place run state can be read from, `local` included).
HOSTED_TARGETS = (TARGET_BYO, TARGET_BOOTSTRAP)
ADMIN_TARGETS = (TARGET_LOCAL, TARGET_BYO, TARGET_BOOTSTRAP)

# Written by `loopy deploy bootstrap` alongside LOOPY_PUBLIC_URL, so `loopy admin
# bootstrap` can print a ready-to-run SSM tunnel command and derive the tunneled
# dashboard URL. CLI-side hints like LOOPY_DEPLOY_TARGET — the engine never reads them.
BOOTSTRAP_INSTANCE_ID_ENV = "LOOPY_BOOTSTRAP_INSTANCE_ID"
BOOTSTRAP_ENGINE_PORT_ENV = "LOOPY_BOOTSTRAP_ENGINE_PORT"

# The pre-rename spelling of the recorded choice, kept readable so a project set up by an
# older CLI resolves without re-running `loopy init`.
LEGACY_DEPLOY_MODE_ENV = "LOOPY_DEPLOY_MODE"
_LEGACY_MODE_TO_TARGET = {"byo": TARGET_BYO, "provisioned": TARGET_BOOTSTRAP}


def resolve_deploy_target(root: str | Path) -> str | None:
    """The recorded deploy target: process env > `loopy.env` > legacy mode > None.

    Same precedence every control-plane var gets (the real environment wins over the
    local dotenv), checked for the current spelling before the legacy one. Returns None
    when unset or unrecognized, so callers fall back to target-agnostic behavior rather
    than trusting a stray value.
    """
    from loopy_runtime.secrets import load_control_plane_env

    file_env = load_control_plane_env(root)

    def lookup(key: str) -> str:
        value = os.environ.get(key, "").strip()
        return (value or file_env.get(key, "").strip()).lower()

    target = lookup(DEPLOY_TARGET_ENV)
    if target in HOSTED_TARGETS:
        return target
    return _LEGACY_MODE_TO_TARGET.get(lookup(LEGACY_DEPLOY_MODE_ENV))


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
