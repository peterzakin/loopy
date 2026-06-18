"""Mint GitHub App installation tokens and inject them into a sandbox.

The runtime's `TokenProvider` seam (see `loopy_runtime.contract`): before a step
runs, mint a short-lived, repo-scoped GitHub App installation token from the
control-plane App credentials and return the env to inject into the sandbox —
the token plus git credential-helper wiring so `git` inside the sandbox
authenticates. The App private key never crosses the boundary; only the
ephemeral token does. This is the runtime counterpart to `loopy auth github`,
which lands the App credentials at the control-plane.

Tokens are cached per provider until shortly before they expire, so a burst of
steps shares one mint rather than re-hitting the API each time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loopy_runtime.manifest_model import SandboxSpec
from loopy_runtime.scm import github_app
from loopy_runtime.scm.github_app import AppCredentials

# Refresh a cached token this long before its real expiry, so a token never
# hands off to the sandbox already on the edge of expiring.
_EXPIRY_MARGIN = timedelta(minutes=2)
# Conservative fallback lifetime when GitHub's response omits/garbles expires_at
# (installation tokens last ~1h; assume less so we refresh early rather than late).
_FALLBACK_LIFETIME = timedelta(minutes=50)


def git_credential_env(token: str) -> dict[str, str]:
    """Env that makes `git` authenticate to github.com with `token`.

    Uses git's env-based config (`GIT_CONFIG_COUNT`/`KEY`/`VALUE`, git >= 2.31) to
    register a per-host credential helper that echoes the token. The token rides a
    separate env var (`GITHUB_TOKEN`) the helper reads, so it never lands in the
    helper's config value (and thus never in `git config --list` output).
    """
    helper = '!f() { echo username=x-access-token; echo "password=${GITHUB_TOKEN}"; }; f'
    return {
        "GITHUB_TOKEN": token,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": helper,
    }


def _parse_expiry(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC) + _FALLBACK_LIFETIME


@dataclass
class _CachedToken:
    token: str
    expires_at: datetime


class GitHubAppTokenProvider:
    """Mint installation tokens from control-plane App credentials, on demand.

    Resolves the installation to use (explicit id, else the App's sole
    installation) and mints a token scoped to `repositories` when given — else the
    whole installation, which already reflects the repos the user picked at install
    time. Network calls are stdlib-`urllib` (sync), run off the event loop.
    """

    def __init__(
        self,
        creds: AppCredentials,
        *,
        installation_id: int | str | None = None,
        repositories: Sequence[str] | None = None,
        base_url: str = github_app.GITHUB_API,
    ):
        self._creds = creds
        self._installation_id = installation_id
        self._repositories = list(repositories) if repositories else None
        self._base_url = base_url
        self._lock = asyncio.Lock()
        self._cache: _CachedToken | None = None

    async def token_env(self, spec: SandboxSpec) -> Mapping[str, str]:
        return git_credential_env(await self._token())

    async def _token(self) -> str:
        async with self._lock:  # one mint at a time; concurrent steps share the result
            now = datetime.now(UTC)
            if self._cache and self._cache.expires_at - _EXPIRY_MARGIN > now:
                return self._cache.token
            token, expires_at = await asyncio.to_thread(self._mint)
            self._cache = _CachedToken(token, expires_at)
            return token

    def _mint(self) -> tuple[str, datetime]:
        installation_id = self._installation_id or self._resolve_installation_id()
        result = github_app.mint_installation_token(
            self._creds,
            installation_id,
            repositories=self._repositories,
            base_url=self._base_url,
        )
        return result["token"], _parse_expiry(result.get("expires_at"))

    def _resolve_installation_id(self) -> int | str:
        installations = github_app.list_installations(self._creds, base_url=self._base_url)
        if not installations:
            raise github_app.GitHubAppError(
                "the GitHub App has no installations — install it on your repos first"
            )
        if len(installations) > 1:
            raise github_app.GitHubAppError(
                "the GitHub App has multiple installations — set installation_id to disambiguate"
            )
        return installations[0]["id"]
