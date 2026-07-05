"""`loopy doctor` — a first-run preflight for a project's *runnability*.

`loopy compile` proves the manifest is **valid**; it says nothing about whether a real run
would succeed. The scaffold (`loopy init`) deliberately compiles green while still carrying a
placeholder that passes a naive presence check but fails the moment an agent actually runs: a
literal `ANTHROPIC_API_KEY=sk-ant-...`. It also catches two repo-shaped traps a project can
fall into — a `repos:` pointed at `octocat/Hello-World` (which you can't push to) and repos
declared with no git auth wired. The runtime's own `preflight()` only checks that a key is
*present*, so the placeholder slips straight through.

`diagnose` is pure (no I/O) so it's unit-testable: it takes the compiled registry, a
`read_env` callback that returns a parsed env_file (or `None` if missing), and the merged
control-plane env, and returns an ordered list of `Finding`s. The CLI does the file I/O and
renders them.

`check_repo_access` is the one live check `diagnose` can't make: with a GitHub App
configured, `diagnose` can only see that *some* App exists — not whether it was actually
installed on the repos in `registry.yml`. `check_repo_access` resolves the installation,
mints a token, and confirms each declared repo is in the App's selected repositories,
catching at preflight the gap that otherwise only surfaced at `loopy run` ("no
installations" / a clone 403). It does network I/O, so it lives apart from pure `diagnose`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from loopy_core.builtins import provider_for

# Built-in providers with a dedicated delivery-wiring path whose absence is already reported
# elsewhere (GitHub by `registration_findings`), so the generic secret check skips them to
# avoid a duplicate finding. The per-provider fix hint for the rest:
_WEBHOOK_SECRET_FIX = {
    "sentry": "run `loopy auth sentry` (or set it in loopy.env)",
    "datadog": "run `loopy auth datadog` (or set it in loopy.env)",
}
_SECRET_CHECK_SKIP = frozenset({"github"})

# The exact values the scaffold writes. Each is *present* (so presence checks pass) but not
# runnable — these are the strings `doctor` exists to catch.
PLACEHOLDER_ANTHROPIC_KEY = "sk-ant-..."
PLACEHOLDER_REPO = "octocat/hello-world"  # compared case-insensitively

# Words/tokens that mark a value as a not-yet-filled-in placeholder rather than a real secret.
# Matched case-insensitively as a substring; see `_placeholder_reason` for the whole heuristic.
_PLACEHOLDER_WORDS = re.compile(
    r"change[-_ ]?me|placeholder|\byour[-_]|\bexample\b|x{4,}|\bto[-_ ]?do\b|"
    r"fix[-_ ]?me|fill[-_ ]?me|replace[-_ ]?me|\bdummy\b|\bsample\b",
    re.IGNORECASE,
)


def _placeholder_reason(value: str) -> tuple[str, str] | None:
    """Heuristic: if `value` looks like an unfilled placeholder, return `(reason, hint)`, else None.

    Deliberately a *heuristic* — a real secret can legitimately contain any of these — so every
    caller reports its hits as `warn`, never `error`. It catches the values that pass a presence
    check but break a real run: a leftover scaffold stub (`sk-ant-...`), an inline comment that a
    literal-value dotenv folds into the value (`ghp_x # my key`), an `<angle-bracket>` template,
    or a giveaway word like `changeme`/`your-token`/`example`.
    """
    v = value.strip()
    if not v:
        return None  # an empty value is "unset", not a placeholder — presence checks own that
    # `#` first: dotenv values are literal (no inline-comment stripping), so a trailing comment
    # silently becomes part of the value — the sneakiest of these, so give it its own hint.
    if "#" in v:
        return (
            "contains '#' — dotenv values are literal, so an inline comment becomes part of "
            "the value",
            "put the comment on its own line, or drop it",
        )
    if "..." in v:
        return ("contains '...' — looks like a leftover scaffold placeholder", "set a real value")
    if "<" in v or ">" in v:
        return ("contains '<'/'>' — looks like a <fill-me-in> placeholder", "set a real value")
    if _PLACEHOLDER_WORDS.search(v):
        return ("looks like a placeholder, not a real value", "set a real value")
    return None


def placeholder_warnings(
    *, read_env: Callable[[str], dict[str, str] | None], env_files: list[str]
) -> list[Finding]:
    """Warn (never error) on env_file values that look like unfilled placeholders.

    Shared by `diagnose` (so `loopy doctor` surfaces it with the other preflight gaps) and the
    `loopy run` startup path (the safety net for a user who skips `doctor`). Scoped to sandbox
    env_files — the dotenv a user hand-edits — not the whole process env, which would drown real
    hits in system noise. Skips the exact `ANTHROPIC_API_KEY` scaffold stub: `diagnose` already
    reports that as an error, so re-warning it would be double-reporting.
    """
    findings: list[Finding] = []
    for rel in env_files:
        env = read_env(rel)
        if not env:
            continue  # missing/empty env_file: diagnose reports the miss; nothing to scan here
        for key, value in env.items():
            if key == "ANTHROPIC_API_KEY" and value == PLACEHOLDER_ANTHROPIC_KEY:
                continue  # already an error in diagnose — don't also warn
            reason = _placeholder_reason(value)
            if reason is not None:
                findings.append(Finding("warn", f"{key} in {rel} {reason[0]}", reason[1]))
    return findings


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
) -> list[Finding]:
    """Return the runnability problems in a compiled project (empty ⇒ ready to run).

    Checks the scaffold defaults that compile clean but break a real run:
      1. a placeholder `ANTHROPIC_API_KEY` (or a referenced env_file that's missing); plus a
         heuristic `warn` for any other env_file value that looks like an unfilled placeholder,
      2. a sandbox still pointing `repos:` at the unpushable starter repo,
      3. no git auth wired (no GitHub App in `loopy.env`, no `GITHUB_TOKEN` in an env_file)
         while sandboxes declare repos that need cloning/pushing,
      4. a `provider: daytona` sandbox with no `DAYTONA_API_KEY` at the control plane — the
         Daytona client reads it from the process env (from `loopy.env`), so a naive presence
         check on the env_file misses it and the run only dies when the sandbox is acquired.
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

    # 1b. Any *other* env_file value that looks like an unfilled placeholder — a leftover stub,
    #     an inline comment folded into the value, an <angle-bracket> template. Heuristic, so
    #     these are warnings (a real secret could contain one of these), unlike the exact key
    #     stub above which is a certain error.
    findings.extend(placeholder_warnings(read_env=read_env, env_files=env_files))

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

    # 4. Daytona provider key — a sandbox on `provider: daytona` needs DAYTONA_API_KEY at the
    #    control plane (loopy.env / the real env), NOT in its env_file. The runtime's preflight
    #    only checks the harness's env_file keys, so a missing Daytona key slips through to the
    #    first sandbox acquire mid-run; flag it here where the other first-run gaps are caught.
    daytona_sandboxes = sorted(
        name for name, sandbox in registry.sandboxes.items() if sandbox.provider == "daytona"
    )
    if daytona_sandboxes and not control_plane_env.get("DAYTONA_API_KEY"):
        is_one = len(daytona_sandboxes) == 1
        findings.append(
            Finding(
                "error",
                f"sandbox '{', '.join(daytona_sandboxes)}' "
                f"{'uses' if is_one else 'use'} provider: daytona but DAYTONA_API_KEY is not set",
                "add DAYTONA_API_KEY to loopy.env (control-plane creds), or export it",
            )
        )

    # 5. Built-in webhook secret — a workflow triggering on a built-in provider's event
    #    (e.g. `on: Sentry.IssueCreated`) needs that provider's signing secret set, or `loopy
    #    run` accepts its deliveries unverified. Only fires for providers actually referenced
    #    (their event is injected into the registry with builtin=True) whose secret is unset.
    #    Warn, not error: the runtime still runs, loudly unverified — matching that posture.
    used_providers = {}
    for evname, event in registry.events.items():
        if getattr(event, "builtin", False):
            provider = provider_for(evname)
            if provider is not None and provider.name not in _SECRET_CHECK_SKIP:
                used_providers[provider.name] = provider
    for name, provider in sorted(used_providers.items()):
        if not control_plane_env.get(provider.secret_env):
            findings.append(
                Finding(
                    "warn",
                    f"{provider.secret_env} is not set — {name} deliveries to "
                    f"{provider.webhook_path} run unverified (dev only)",
                    _WEBHOOK_SECRET_FIX.get(name, f"set {provider.secret_env} in loopy.env"),
                )
            )

    return findings


def _declared_repo_slugs(registry) -> list[str]:  # noqa: ANN001 - registry.model.Registry
    """The repos sandboxes actually clone, as deduped lowercase `owner/name`.

    Skips the unpushable starter repo — `diagnose` already flags that as its own error, so
    re-reporting it as "not in the App's repos" would just be noise.
    """
    slugs: list[str] = []
    for sandbox in registry.sandboxes.values():
        for repo in sandbox.repos:
            slug = _repo_slug(repo.url)
            if slug != PLACEHOLDER_REPO and slug not in slugs:
                slugs.append(slug)
    return slugs


def check_repo_access(registry, creds) -> list[Finding]:  # noqa: ANN001 - Registry, AppCredentials
    """Live preflight: confirm every repo a sandbox clones is in the App's selected repos.

    The gap `diagnose` can't see: a configured GitHub App that was never installed on the
    repos in `registry.yml` (or installed on the wrong ones) compiles and diagnoses clean,
    then fails at `loopy run` with "no installations" or a clone 403. This makes the call
    `diagnose` can't — resolve the installation, mint a token, list the repos it reaches —
    and flags any declared repo the App can't access.

    Network calls go through `loopy_runtime.scm.github_app` (stubbed in tests). Any GitHub or
    network error degrades to a single `warn` rather than failing the whole preflight — the
    run path still surfaces the real error loudly. Returns `Finding`s; empty ⇒ all reachable.
    """
    from loopy_runtime.scm import github_app

    wanted = _declared_repo_slugs(registry)
    if not wanted:
        return []  # no repos cloned → nothing to check (repo-less project)

    def _warn(detail: str) -> list[Finding]:
        return [
            Finding("warn", f"couldn't verify repo access: {detail}", "run `loopy auth github`")
        ]

    try:
        installations = github_app.list_installations(creds)
    except (github_app.GitHubAppError, OSError) as exc:
        return _warn(str(exc))

    if not installations:
        return [
            Finding(
                "error",
                "the GitHub App is configured but not installed on any account yet",
                "install it — run `loopy auth github` for the URL, then pick your repos",
            )
        ]
    if len(installations) > 1:
        return _warn("the App has multiple installations — can't tell which serves these repos")

    try:
        token = github_app.mint_installation_token(creds, installations[0]["id"])["token"]
        reachable_payload = github_app.list_installation_repositories(token)
    except (github_app.GitHubAppError, OSError) as exc:
        return _warn(str(exc))

    reachable = {
        str(repo.get("full_name", "")).lower() for repo in reachable_payload.get("repositories", [])
    }
    missing = [slug for slug in wanted if slug not in reachable]
    if not missing:
        return []
    is_one = len(missing) == 1
    return [
        Finding(
            "error",
            f"the GitHub App can't access {', '.join(missing)} — "
            f"{'it is' if is_one else 'they are'} not in its selected repositories",
            "add the repo(s) to the App's installation (run `loopy auth github` for the URL)",
        )
    ]
