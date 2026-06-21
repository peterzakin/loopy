"""GitHub App credential mechanics — JWT, installation discovery, token minting.

The bring-your-own GitHub App model (see plans/future/repo-access): each
deployment registers its *own* App. Minting installation tokens is pure
client-side — App private key → RS256 JWT → installation token via
`api.github.com` — so loopy needs no central app and no persistent server.

The powerful long-lived secret (the App private key) lives at the
control-plane and never enters a sandbox; only the ephemeral, repo-scoped
installation token does. These helpers are reused by the `loopy auth github`
onboarding flow (CLI) today and by the runtime TokenProvider next milestone.

Network calls go through a thin stdlib-`urllib` client so the runtime gains no
new HTTP dependency; RS256 signing is the one third-party dep (`pyjwt[crypto]`),
imported lazily so importing this module stays cheap.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GITHUB_API = "https://api.github.com"

# GitHub caps App JWTs at 10 minutes; we use 9 to leave headroom, and backdate
# `iat` by 60s so minor control-plane/GitHub clock skew can't reject the token.
_JWT_TTL_SECONDS = 540
_JWT_BACKDATE_SECONDS = 60

# Network resilience. A stalled connection must not hang `doctor`/`run` forever, and a
# single truncated body or transient 5xx — likely mid-pagination on a large installation —
# must not kill a whole preflight. Cap the wait, then retry transient failures a few times
# with exponential backoff. Applies to every request, so pagination is covered page-by-page.
_REQUEST_TIMEOUT_SECONDS = 30
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.5


class GitHubAppError(Exception):
    """Base error for the GitHub App credential layer."""


class MissingCredentials(GitHubAppError):
    """No usable App credentials were found (id and/or private key absent)."""


class GitHubAPIError(GitHubAppError):
    """A GitHub API call returned a non-2xx response."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"GitHub API error {status}: {detail}")


@dataclass(frozen=True)
class AppCredentials:
    """A GitHub App's identity: its numeric id and PEM private key.

    This is the control-plane secret. Keep it off any sandbox — pass only the
    minted installation token across the trust boundary.
    """

    app_id: str
    private_key_pem: str

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, root: str | Path = ".") -> AppCredentials:
        """Build credentials from a control-plane env map.

        Reads `GITHUB_APP_ID` plus the private key, preferring an inline
        `GITHUB_APP_PRIVATE_KEY` (what `loopy auth github` writes — newlines escaped to
        survive the dotenv) and falling back to a `GITHUB_APP_PRIVATE_KEY_FILE` path
        (resolved relative to `root`, for production secret mounts). Prefer the inline
        form: a path resolves against whichever `--root` a later command uses, which is a
        silent footgun. Raises `MissingCredentials` with an actionable message when
        something's absent.
        """
        app_id = env.get("GITHUB_APP_ID")
        if not app_id:
            raise MissingCredentials("GITHUB_APP_ID is not set")
        pem = env.get("GITHUB_APP_PRIVATE_KEY")
        if pem:
            # An inline key is stored single-line with newlines escaped (the dotenv parser
            # has no multi-line values); restore them. A genuine multi-line PEM — pasted
            # straight into a real env var — has no literal "\n", so this is a no-op for it.
            pem = pem.replace("\\n", "\n")
        else:
            key_file = env.get("GITHUB_APP_PRIVATE_KEY_FILE")
            if not key_file:
                raise MissingCredentials(
                    "set GITHUB_APP_PRIVATE_KEY_FILE (or inline GITHUB_APP_PRIVATE_KEY)"
                )
            path = Path(key_file)
            if not path.is_absolute():
                path = Path(root) / path
            if not path.is_file():
                raise MissingCredentials(f"private key file not found: {path}")
            pem = path.read_text()
        return cls(app_id=str(app_id), private_key_pem=pem)


def app_jwt(creds: AppCredentials, *, now: datetime | None = None) -> str:
    """Sign a short-lived App JWT (RS256) for authenticating as the App itself."""
    import jwt  # lazy: pulls in cryptography; not needed for a bare import of this module

    issued = int((now or datetime.now(UTC)).timestamp())
    payload = {
        "iat": issued - _JWT_BACKDATE_SECONDS,
        "exp": issued + _JWT_TTL_SECONDS,
        "iss": creds.app_id,
    }
    return jwt.encode(payload, creds.private_key_pem, algorithm="RS256")


def _request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: object | None = None,
) -> dict:
    """Issue one GitHub API request and parse the JSON body.

    The single network boundary of this module — tests stub this rather than
    hitting the wire. Raises `GitHubAPIError` on a non-2xx response.
    """
    request_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "loopy-auth-github",
    }
    if headers:
        request_headers.update(headers)
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    for attempt in range(_MAX_ATTEMPTS):
        last = attempt == _MAX_ATTEMPTS - 1
        try:
            with urllib.request.urlopen(  # noqa: S310 - https GitHub API only
                request, timeout=_REQUEST_TIMEOUT_SECONDS
            ) as response:
                body = response.read().decode()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            # A real HTTP response. 5xx is a transient server-side fault worth retrying;
            # a 4xx is a client error that won't fix itself, so surface it immediately.
            detail = exc.read().decode(errors="replace") if exc.fp else exc.reason
            if exc.code < 500 or last:
                raise GitHubAPIError(exc.code, detail) from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError) as exc:
            # Transport-level failure with no usable HTTP status: a refused/stalled/timed-out
            # connection (URLError, and the socket timeout urlopen raises), or a truncated body
            # (http.client.IncompleteRead and friends are HTTPException — crucially *not*
            # OSError, so a caller's `except OSError` would miss them). Normalize to
            # GitHubAppError so every caller catches it through the App error hierarchy.
            if last:
                raise GitHubAppError(f"GitHub request {method} {url} failed: {exc}") from exc
        time.sleep(_RETRY_BACKOFF_SECONDS * (2**attempt))
    # The loop always returns or raises on its final attempt; this is unreachable.
    raise GitHubAppError("GitHub request failed after retries")  # pragma: no cover


def _bearer(creds: AppCredentials) -> dict[str, str]:
    return {"Authorization": f"Bearer {app_jwt(creds)}"}


def exchange_manifest_code(code: str, *, base_url: str = GITHUB_API) -> dict:
    """Trade a manifest-flow `code` for the created App's credentials.

    Returns GitHub's conversion payload: `{id, pem, client_id, slug, ...}`.
    This is the unauthenticated step right after the user creates the App.
    """
    return _request_json("POST", f"{base_url}/app-manifests/{code}/conversions")


def find_installation(
    creds: AppCredentials, owner: str, repo: str, *, base_url: str = GITHUB_API
) -> dict:
    """Look up the App's installation on a specific repo (App-JWT auth)."""
    return _request_json(
        "GET", f"{base_url}/repos/{owner}/{repo}/installation", headers=_bearer(creds)
    )


def list_installations(creds: AppCredentials, *, base_url: str = GITHUB_API) -> list[dict]:
    """List every installation of the App (App-JWT auth)."""
    result = _request_json("GET", f"{base_url}/app/installations", headers=_bearer(creds))
    return result if isinstance(result, list) else []


def mint_installation_token(
    creds: AppCredentials,
    installation_id: int | str,
    *,
    repositories: Sequence[str] | None = None,
    permissions: Mapping[str, str] | None = None,
    base_url: str = GITHUB_API,
) -> dict:
    """Mint a short-lived installation token, optionally narrowed to repos/perms.

    The returned `{token, expires_at, ...}` is the only credential that should
    ever cross into a sandbox. With no `repositories`/`permissions` the token
    spans the whole installation; pass them to scope it down (least privilege).
    """
    payload: dict[str, object] = {}
    if repositories is not None:
        payload["repositories"] = list(repositories)
    if permissions is not None:
        payload["permissions"] = dict(permissions)
    return _request_json(
        "POST",
        f"{base_url}/app/installations/{installation_id}/access_tokens",
        headers=_bearer(creds),
        payload=payload or None,
    )


def list_installation_repositories(token: str, *, base_url: str = GITHUB_API) -> dict:
    """List the repos an installation token can reach (`{total_count, repositories}`).

    Walks every page of `/installation/repositories`. GitHub paginates this at 30
    repos/page by default; we ask for 100 and accumulate until we've collected
    `total_count`. Skipping this is a silent footgun: an installation set to "all
    repositories" on an account with more repos than one page holds would report
    only the first slice, so the doctor preflight flags perfectly reachable repos
    as "not in its selected repositories".
    """
    headers = {"Authorization": f"token {token}"}
    repositories: list[dict] = []
    total_count = 0
    page = 1
    while True:
        payload = _request_json(
            "GET",
            f"{base_url}/installation/repositories?per_page=100&page={page}",
            headers=headers,
        )
        total_count = payload.get("total_count", total_count)
        batch = payload.get("repositories") or []
        repositories.extend(batch)
        # Stop once we've seen every repo GitHub promised, or a short/empty page
        # (the latter guards against a missing/zero total_count).
        if not batch or len(repositories) >= total_count:
            break
        page += 1
    return {"total_count": total_count, "repositories": repositories}
