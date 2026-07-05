"""Sandbox env passthrough — the naming rules shared by the compiler and the runtime.

A sandbox's `env:` list names the environment variables the engine forwards into that
sandbox. The values are *not* stored here: in local dev they come from the sandbox's
`env_file`, and in production from the engine's own process environment under a
per-sandbox namespace. This module owns the two naming rules that both sides must agree
on, so the compiler can validate what the runtime will later resolve:

  * `sandbox_env_prefix(name)` — the production namespace for a sandbox. On the platform
    the engine environment carries `<PREFIX>_<KEY>` (e.g. `BASESANDBOX_ANTHROPIC_API_KEY`);
    the runtime strips the prefix so the agent sees the canonical `<KEY>`. The prefix is
    derived from the sandbox name so the platform variable list is self-documenting: every
    var names the sandbox it feeds.
  * the reserved set — control-plane credentials that must never be declared as sandbox
    passthrough. These live in the engine's environment alongside the sandbox vars, so
    forwarding one into an (untrusted) sandbox would leak an infra secret. The compiler
    rejects them (LOOPY-E216); this is the denylist it checks against.

`loopy_runtime` imports `sandbox_env_prefix` to resolve values at run time. Keeping both
the prefix rule and the denylist in `loopy_core` means the compile-time gate and the
run-time resolution can never disagree on the mapping.
"""

from __future__ import annotations

import re

# Reserved namespace for the engine's own configuration. Any key with this prefix is
# control-plane (LOOPY_ADMIN_TOKEN, LOOPY_ADMIN_TOKEN_NEXT, LOOPY_PUBLIC_URL, and anything
# added later), so it can never be a sandbox passthrough key.
RESERVED_ENV_PREFIX = "LOOPY_"

# Control-plane credentials the engine needs to boot and to reach its infra. They share the
# engine's process environment with sandbox vars, so a sandbox must not be able to name one
# in `env:` and have it forwarded into an untrusted container. (`GITHUB_TOKEN` is absent on
# purpose: it is a legitimate per-sandbox git credential, unlike the App private key.)
RESERVED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DAYTONA_API_KEY",
        "DAYTONA_API_URL",
        "REDIS_URL",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "SENTRY_WEBHOOK_SECRET",
    }
)

# A POSIX-shaped environment variable name: a letter or underscore, then letters, digits, or
# underscores. This also rejects wildcards/globs (`*`, `?`) and dotted paths, so there is no
# "forward everything" escape hatch — each key must be named explicitly.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_NON_IDENTIFIER_RE = re.compile(r"[^0-9A-Za-z]")


def is_valid_env_name(key: str) -> bool:
    """True if `key` is a well-formed environment variable name (and thus not a wildcard)."""
    return bool(_ENV_NAME_RE.match(key))


def is_reserved_env_key(key: str) -> bool:
    """True if `key` is a control-plane credential a sandbox must not declare in `env:`."""
    return key.startswith(RESERVED_ENV_PREFIX) or key in RESERVED_ENV_KEYS


def sandbox_env_prefix(name: str) -> str:
    """The production env-var namespace for a sandbox: uppercased, non-alphanumerics to `_`.

    `BaseSandbox` -> `BASESANDBOX`, `code-fixer` -> `CODE_FIXER`, `default` -> `DEFAULT`. The
    engine reads `<PREFIX>_<KEY>` from its environment and injects `<KEY>` into the sandbox.
    Distinct sandbox names can collide here (e.g. `Web-app` and `Web_app`); the compiler
    rejects such a collision (LOOPY-E217) when either sandbox uses `env:`.
    """
    return _NON_IDENTIFIER_RE.sub("_", name).upper()
