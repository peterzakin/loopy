"""Clone a sandbox's declared `repos:` into its workspace at acquire time.

Sandboxes come up with an empty filesystem; coding agents need a git checkout to work
against. This runs `git clone` through the `Sandbox.exec` seam every provider already
implements, so one helper serves local/docker/daytona. Auth rides the credential env the
runtime injects (`GITHUB_TOKEN` + the `GIT_CONFIG_*` helper from the `TokenProvider`), so
private repos clone without any extra wiring here.

Repos clone into a destination *relative* to wherever `exec` runs (local: the temp
workdir; docker: `-w` workdir; daytona: its default cwd). The agent harness runs through
that same `exec`, so it sees the checkout at `./<dest>` regardless of the absolute path —
no provider-specific workdir resolution needed.
"""

from __future__ import annotations

from collections.abc import Iterable

from loopy_runtime.contract import Sandbox
from loopy_runtime.manifest_model import RepoSpec

_GITHUB = "https://github.com/"


def normalize_repo_url(url: str) -> str:
    """`owner/name` → a clonable https GitHub URL; pass full URLs / scp-style refs through."""
    if "://" in url or url.startswith("git@"):
        return url
    owner_name = url.removesuffix(".git").strip("/")
    return f"{_GITHUB}{owner_name}.git"


def repo_dest(repo: RepoSpec) -> str:
    """The checkout directory: explicit `path`, else the repo name from the URL."""
    if repo.path:
        return repo.path
    name = normalize_repo_url(repo.url).rstrip("/").rsplit("/", 1)[-1]
    return name.removesuffix(".git") or "repo"


async def provision_workspace(sandbox: Sandbox, repos: Iterable[RepoSpec]) -> None:
    """Clone each repo into the sandbox workspace, checking out `ref` when given.

    Raises on the first failed clone/checkout so the caller can release the sandbox and let
    the step fail loudly rather than handing the agent a half-built workspace.
    """
    for repo in repos:
        dest = repo_dest(repo)
        argv = ["git", "clone"]
        if repo.depth:
            argv += ["--depth", str(repo.depth)]
            # A shallow clone only fetches one tip, so the ref must be selected at clone time
            # via --branch (a branch or tag — a bare SHA needs full history; see below).
            if repo.ref:
                argv += ["--branch", repo.ref]
        argv += [normalize_repo_url(repo.url), dest]

        result = await sandbox.exec(argv)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"clone {repo.url!r} failed: {detail}")

        # Full clone (depth=None): the ref may be a branch, tag, or SHA, all reachable now.
        if repo.ref and not repo.depth:
            checkout = await sandbox.exec(["git", "-C", dest, "checkout", repo.ref])
            if checkout.exit_code != 0:
                detail = (checkout.stderr or checkout.stdout).strip()
                raise RuntimeError(f"checkout {repo.ref!r} in {repo.url!r} failed: {detail}")
