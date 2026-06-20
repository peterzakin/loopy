"""Source-control credential mechanics.

Today this is GitHub-only (`github_app`): the bring-your-own GitHub App model
where the long-lived App private key stays at the control-plane and only
short-lived, repo-scoped installation tokens ever cross into a sandbox.
"""

from __future__ import annotations

from loopy_runtime.scm.github_app import (
    GITHUB_API,
    AppCredentials,
    GitHubAPIError,
    GitHubAppError,
    MissingCredentials,
    app_jwt,
    exchange_manifest_code,
    find_installation,
    get_pull_request,
    list_installation_repositories,
    list_installations,
    mint_installation_token,
)

__all__ = [
    "GITHUB_API",
    "AppCredentials",
    "GitHubAPIError",
    "GitHubAppError",
    "MissingCredentials",
    "app_jwt",
    "exchange_manifest_code",
    "find_installation",
    "get_pull_request",
    "list_installation_repositories",
    "list_installations",
    "mint_installation_token",
]
