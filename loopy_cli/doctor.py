"""`loopy doctor` — a first-run preflight for a project's *runnability*.

`loopy compile` proves the manifest is **valid**; it says nothing about whether a real run
would succeed. The scaffold (`loopy init`) deliberately compiles green while still carrying
placeholders that pass a naive presence check but fail the moment an agent actually runs: a
literal `ANTHROPIC_API_KEY=sk-ant-...`, a `repos:` pointed at `octocat/Hello-World` (which you
can't push to), and no git auth wired yet. The runtime's own `preflight()` only checks that a
key is *present*, so the placeholder slips straight through.

`diagnose` is pure (no I/O) so it's unit-testable: it takes the compiled registry, a
`read_env` callback that returns a parsed env_file (or `None` if missing), and the merged
control-plane env, and returns an ordered list of `Finding`s. The CLI does the file I/O and
renders them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

# The exact values the scaffold writes. Each is *present* (so presence checks pass) but not
# runnable — these are the strings `doctor` exists to catch.
PLACEHOLDER_ANTHROPIC_KEY = "sk-ant-..."
PLACEHOLDER_REPO = "octocat/hello-world"  # compared case-insensitively


@dataclass(frozen=True)
class Finding:
    """One preflight result. `level` is `error` (blocks a run) or `warn` (likely to)."""

    level: str  # "error" | "warn"
    message: str
    hint: str | None = None


def _repo_slug(url: str) -> str:
    """Normalize a `Repo.url` to a lowercase `owner/name` for comparison.

    Accepts the `owner/name` shorthand or a full https URL; strips a trailing `.git`.
    """
    slug = url.strip().lower()
    for prefix in ("https://github.com/", "git@github.com:", "http://github.com/"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    return slug.strip("/")


def diagnose(
    registry,  # noqa: ANN001 - loopy_core.registry.model.Registry
    *,
    read_env: Callable[[str], dict[str, str] | None],
    control_plane_env: Mapping[str, str],
    is_tracked: Callable[[str], bool] | None = None,
) -> list[Finding]:
    """Return the runnability problems in a compiled project (empty ⇒ ready to run).

    Checks the scaffold/setup defaults that compile clean but break (or endanger) a real run:
      1. a placeholder `ANTHROPIC_API_KEY` (or a referenced env_file that's missing),
      2. a sandbox still pointing `repos:` at the unpushable starter repo,
      3. no git auth wired (no GitHub App in `loopy.env`, no `GITHUB_TOKEN` in an env_file)
         while sandboxes declare repos that need cloning/pushing,
      4. an env_file holding live secrets that's tracked by git (`is_tracked`, when supplied) —
         a guardrail, since `.gitignore` is the only thing keeping real keys off a commit.

    `is_tracked(rel)` answers "is this project-relative path tracked by git?"; it's injected
    (the CLI shells out to `git`) so `diagnose` itself stays pure and unit-testable. Omitted ⇒
    the tracking guardrail is skipped.
    """
    findings: list[Finding] = []

    # Dedup env_files across sandboxes, preserving first-seen order so output is stable.
    env_files: list[str] = []
    for sandbox in registry.sandboxes.values():
        for rel in sandbox.env_file:
            if rel not in env_files:
                env_files.append(rel)

    # 1. Model auth — the placeholder key, and any referenced-but-missing env_file.
    placeholder_in: list[str] = []
    token_in_env_file = False
    for rel in env_files:
        env = read_env(rel)
        if env is None:
            findings.append(
                Finding(
                    "error",
                    f"sandbox env_file '{rel}' is referenced in registry.yml but missing",
                    "create it (a fresh `loopy init` writes one) or fix the env_file: path",
                )
            )
            continue
        if env.get("ANTHROPIC_API_KEY") == PLACEHOLDER_ANTHROPIC_KEY:
            placeholder_in.append(rel)
        if env.get("GITHUB_TOKEN"):
            token_in_env_file = True
    if placeholder_in:
        findings.append(
            Finding(
                "error",
                f"ANTHROPIC_API_KEY is still the scaffold placeholder "
                f"({PLACEHOLDER_ANTHROPIC_KEY}) in {', '.join(placeholder_in)}",
                "set a real key, or rely on Claude Code OAuth reachable via the sandbox HOME",
            )
        )

    # 1b. Live secrets committed to git. An env_file carries real keys — most acutely the
    # API-key-authed Anthropic case, where the value is on disk in cleartext — and `.gitignore`
    # is the only guard. It's easy to `git add` the file before (or instead of) gitignoring it,
    # so warn if any referenced env_file is tracked. A guardrail, not a blocker (warn, not error).
    if is_tracked is not None:
        for rel in env_files:
            if is_tracked(rel):
                findings.append(
                    Finding(
                        "warn",
                        f"env_file '{rel}' is tracked by git — it holds live secrets",
                        f"untrack it: add '{rel}' to .gitignore, then `git rm --cached {rel}`",
                    )
                )

    # 2. The unpushable starter repo.
    starter_sandboxes: list[str] = []
    declares_repos = False
    for name, sandbox in registry.sandboxes.items():
        for repo in sandbox.repos:
            declares_repos = True
            if _repo_slug(repo.url) == PLACEHOLDER_REPO:
                starter_sandboxes.append(name)
                break
    if starter_sandboxes:
        findings.append(
            Finding(
                "error",
                f"sandbox '{', '.join(sorted(set(starter_sandboxes)))}' still points repos: at "
                f"the starter repo you can't push to",
                "change repos: to a repo you can push to (you/your-repo, or a fork)",
            )
        )

    # 3. Git auth — only a problem if some sandbox actually clones repos.
    app_configured = bool(control_plane_env.get("GITHUB_APP_ID"))
    if declares_repos and not app_configured and not token_in_env_file:
        findings.append(
            Finding(
                "warn",
                "no git auth configured — sandboxes clone repos but no GitHub App is set up "
                "and no GITHUB_TOKEN is in an env_file",
                "run `loopy auth github` (recommended), or add GITHUB_TOKEN to the env_file",
            )
        )

    return findings
