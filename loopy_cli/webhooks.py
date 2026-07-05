"""`loopy webhooks` — wire external services' webhooks to this project's sensors.

The engine's half of a webhook already exists: `loopy run` serves every
`@sensor(webhook=...)` path (and the built-in `/hooks/github`). This module owns the
*other* half — telling the outside service where to deliver. Registration is
deliberately per-service, mirroring `loopy auth`: each service that supports
programmatic registration gets its own command with its own mechanics
(`loopy webhooks github` creates repo webhooks via the GitHub App), and a service
that doesn't simply never gets one — for those, `loopy webhooks list` prints each
endpoint's public delivery URL to paste into that service's settings by hand.

GitHub registration is repo-level on purpose. A GitHub App's own webhook can only be
wired at App creation (its event subscriptions have no update API), which would break
the "register later" path for anyone who had no public URL at auth time. Repo hooks
(`POST /repos/{owner}/{repo}/hooks`, via the App's `repository_hooks: write`
permission) work at any time and are precise to the repos in `registry.yml`.

Heavy imports (compiler, runtime, network) are deferred into command bodies so
`loopy compile` stays runtime-free.
"""

from __future__ import annotations

import os
import secrets as secrets_module
from dataclasses import dataclass, field
from pathlib import Path

import typer

from loopy_cli.doctor import Finding, _declared_repo_slugs

webhooks_app = typer.Typer(
    no_args_is_help=True, help="Register and inspect this project's webhook endpoints."
)

# The single URL GitHub delivers every event type to (the compiler's built-in sensors
# and the `examples/github` sensors all share it).
GITHUB_HOOK_PATH = "/hooks/github"

# Built-in loopy event -> the GitHub webhook event name whose deliveries carry it.
# Kept in lockstep with `loopy_core.builtins.GITHUB_EVENTS` (test-asserted), so a new
# built-in can't ship without saying which subscription its registration needs.
BUILTIN_HOOK_EVENTS: dict[str, str] = {
    "Github.PullRequestOpened": "pull_request",
    "Github.PullRequestMerged": "pull_request",
    "Github.IssueOpened": "issues",
    "Github.IssueCommentCreated": "issue_comment",
    "Github.Push": "push",
}
ALL_HOOK_EVENTS = sorted(set(BUILTIN_HOOK_EVENTS.values()))


def normalize_public_url(raw: str) -> str:
    """Validate and canonicalize a public webhook base URL.

    Delivery URLs are built as `<base> + <sensor path>` (e.g. `<base>/hooks/github`), so the
    base must be a bare http(s) origin (an optional path prefix is fine) with no trailing
    slash. A scheme-less host is assumed https — the common case for a tunnel or deployed
    hostname pasted without one. Raises ValueError with the reason on unusable input.
    """
    from urllib.parse import urlparse

    candidate = raw.strip()
    if any(ch.isspace() for ch in candidate):
        raise ValueError("the URL must not contain whitespace")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme '{parsed.scheme}' — use http:// or https://")
    if not parsed.netloc:
        raise ValueError("the URL needs a host, e.g. https://loopy.example.com")
    return candidate.rstrip("/")


def resolve_public_url(root: Path) -> str | None:
    """The public webhook base for this project, or None if unset.

    Reads `LOOPY_PUBLIC_URL` from the real environment first (the production mechanism),
    then `loopy.env` (the local-dev convenience `loopy init` writes) — the same precedence
    every control-plane var gets. Trailing slash stripped; empty means unset.
    """
    from loopy_runtime.secrets import load_control_plane_env

    url = os.environ.get("LOOPY_PUBLIC_URL", "").strip()
    if not url:
        url = load_control_plane_env(root).get("LOOPY_PUBLIC_URL", "").strip()
    return url.rstrip("/") or None


def github_hook_events(sensors) -> list[str]:  # noqa: ANN001 - list[loopy_core Sensor]
    """The GitHub webhook event names this project's `/hooks/github` sensors need.

    Built-in sensors map exactly (the event they emit names the GitHub event that
    delivers it). A custom sensor on the shared path could need anything, so its
    presence widens to the full built-in set — over-delivering is harmless (every
    mapper returns None for payloads that aren't its concern), under-delivering is a
    silent gap. A project with no GitHub sensors at all also gets the full set: the
    registration is forward-looking wiring, so a later-added `on: Github.*` trigger
    starts firing without a re-registration.
    """
    events: set[str] = set()
    custom = False
    for sensor in sensors:
        if sensor.trigger.kind != "webhook" or sensor.trigger.path != GITHUB_HOOK_PATH:
            continue
        if sensor.source == "builtin" and sensor.emits in BUILTIN_HOOK_EVENTS:
            events.add(BUILTIN_HOOK_EVENTS[sensor.emits])
        else:
            custom = True
    if custom or not events:
        return list(ALL_HOOK_EVENTS)
    return sorted(events)


@dataclass(frozen=True)
class HookSync:
    """One repo's outcome: created/updated (wrote), ok/stale/missing (--check), error."""

    repo: str
    action: str  # "created" | "updated" | "ok" | "stale" | "missing" | "error"
    detail: str = ""


@dataclass(frozen=True)
class SyncReport:
    results: list[HookSync] = field(default_factory=list)
    secret_written: bool = False  # a fresh GITHUB_WEBHOOK_SECRET landed in loopy.env


def _load_creds(root: Path):  # -> AppCredentials; raises MissingCredentials
    """App credentials from loopy.env merged under the process env (env wins)."""
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import load_control_plane_env

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return github_app.AppCredentials.from_env(merged, root=root)


def sync_github_webhooks(
    root: Path,
    *,
    repos: list[str],
    events: list[str],
    public_url: str,
    check: bool = False,
) -> SyncReport:
    """Idempotently point every repo's webhook at this project's `/hooks/github`.

    For each `owner/name`: resolve the App's installation on it, mint a token
    (cached per installation), and converge — a hook already delivering to our URL is
    updated in place (events, secret, active), anything else gets one created.
    `check=True` reports (`ok`/`stale`/`missing`) without writing anything, GitHub or
    local. The signing secret is `GITHUB_WEBHOOK_SECRET` from the merged control-plane
    env when present; otherwise one is generated and written to `loopy.env` after the
    first successful write, so signature verification works with no manual step.
    GitHub never returns hook secrets, so a non-check sync always (re)sends ours —
    convergence, not comparison. Per-repo API failures land as `error` results (with
    a 403 mapped to the missing-Webhooks-permission fix); they never abort the rest.
    Raises `MissingCredentials` when no GitHub App is configured at all.
    """
    from loopy_runtime.scm import github_app
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    creds = _load_creds(root)

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    secret = merged.get("GITHUB_WEBHOOK_SECRET", "").strip()
    generated = False
    if not secret and not check:
        secret = secrets_module.token_hex(32)
        generated = True

    delivery_url = public_url.rstrip("/") + GITHUB_HOOK_PATH
    wanted_events = sorted(events)
    results: list[HookSync] = []
    wrote_any = False
    tokens: dict[object, str] = {}  # installation id -> minted token

    def _describe(exc: github_app.GitHubAppError) -> str:
        status = getattr(exc, "status", None)
        if status == 403:
            return (
                "the App lacks the Webhooks repository permission — add it under the App's "
                "Permissions & events on GitHub (then approve on the installation), or "
                "recreate the App with `loopy auth github --force`"
            )
        if status == 404:
            return "the App is not installed on this repo — run `loopy auth github` for the URL"
        return str(exc)

    for slug in repos:
        owner, _, name = slug.partition("/")
        try:
            installation = github_app.find_installation(creds, owner, name)
            install_id = installation.get("id")
            token = tokens.get(install_id)
            if token is None:
                token = github_app.mint_installation_token(creds, install_id)["token"]
                tokens[install_id] = token
            hooks = github_app.list_repo_hooks(token, owner, name)
            existing = next(
                (h for h in hooks if (h.get("config") or {}).get("url") == delivery_url), None
            )
            if check:
                if existing is None:
                    results.append(
                        HookSync(slug, "missing", f"no webhook delivers to {delivery_url}")
                    )
                elif sorted(existing.get("events") or []) != wanted_events or not existing.get(
                    "active", False
                ):
                    results.append(
                        HookSync(slug, "stale", "registered, but events/active are out of date")
                    )
                else:
                    results.append(HookSync(slug, "ok", f"delivers to {delivery_url}"))
                continue
            if existing is None:
                github_app.create_repo_hook(
                    token, owner, name, url=delivery_url, secret=secret, events=wanted_events
                )
                results.append(HookSync(slug, "created", f"→ {delivery_url}"))
            else:
                github_app.update_repo_hook(
                    token,
                    owner,
                    name,
                    existing["id"],
                    url=delivery_url,
                    secret=secret,
                    events=wanted_events,
                )
                results.append(HookSync(slug, "updated", f"→ {delivery_url}"))
            wrote_any = True
        except (github_app.GitHubAppError, OSError) as exc:
            detail = _describe(exc) if isinstance(exc, github_app.GitHubAppError) else str(exc)
            results.append(HookSync(slug, "error", detail))

    secret_written = False
    if generated and wrote_any:
        # Persist only once a hook actually carries this secret — a generated-but-unused
        # secret in loopy.env would make `run` verify signatures GitHub never signs with it.
        write_control_plane_env(root, {"GITHUB_WEBHOOK_SECRET": secret})
        secret_written = True

    return SyncReport(results=results, secret_written=secret_written)


def _compile_or_exit(root: Path):  # noqa: ANN202 - loopy_core Project
    """Compile `root`, printing diagnostics; exit non-zero unless it compiles clean."""
    from loopy_core.compile.pipeline import compile_project

    result = compile_project(root)
    if result.diagnostics.has_errors() or result.project is None:
        for diagnostic in result.diagnostics.items:
            typer.echo(diagnostic.render(), err=True)
        typer.echo(
            typer.style(
                f"  ✗  fix compile errors first (loopy compile {root})", fg=typer.colors.RED
            ),
            err=True,
        )
        raise typer.Exit(code=1)
    return result.project


_SYNC_MARKS = {
    "created": ("✓", typer.colors.GREEN),
    "updated": ("✓", typer.colors.GREEN),
    "ok": ("✓", typer.colors.GREEN),
    "stale": ("!", typer.colors.YELLOW),
    "missing": ("!", typer.colors.YELLOW),
    "error": ("✗", typer.colors.RED),
}


def render_sync_report(report: SyncReport) -> bool:
    """Print per-repo results (+ the secret note); return True if anything needs action."""
    for result in report.results:
        mark, color = _SYNC_MARKS[result.action]
        line = f"{mark}  {result.repo}: {result.action}"
        if result.detail:
            line += f" — {result.detail}"
        typer.echo("  " + typer.style(line, fg=color))
    if report.secret_written:
        typer.echo(
            "  "
            + typer.style("✓", fg=typer.colors.GREEN)
            + " wrote GITHUB_WEBHOOK_SECRET to loopy.env — `loopy run` now verifies deliveries"
        )
    return any(r.action in ("missing", "stale", "error") for r in report.results)


@webhooks_app.command()
def github(
    root: Path = typer.Option(
        Path("."), "--root", help="Project root (registry.yml + loopy.env)."
    ),
    url: str | None = typer.Option(
        None, "--url", help="Public base URL (default: LOOPY_PUBLIC_URL from loopy.env/env)."
    ),
    check: bool = typer.Option(
        False, "--check", help="Report what's registered on GitHub; change nothing."
    ),
) -> None:
    """Register this project's GitHub webhooks (or `--check` what's registered).

    Creates or updates a webhook on each repo in registry.yml pointing at
    `LOOPY_PUBLIC_URL + /hooks/github`, subscribed to the GitHub events the project's
    sensors need — the wiring that makes built-in `Github.*` triggers actually fire.
    Authenticates as the GitHub App from `loopy auth github` (which needs the Webhooks
    repository permission). Idempotent: re-run after adding a repo, changing the public
    URL, or adding a `Github.*` trigger.
    """
    from loopy_runtime.scm.github_app import MissingCredentials

    project = _compile_or_exit(root)
    repos = _declared_repo_slugs(project.registry)
    if not repos:
        typer.echo(
            "error: no repos declared in registry.yml (sandboxes.*.repos) — GitHub webhooks "
            "are registered per repo, so there's nothing to register on.",
            err=True,
        )
        raise typer.Exit(code=1)

    if url is not None:
        try:
            public_url = normalize_public_url(url)
        except ValueError as exc:
            typer.echo(f"error: --url: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    else:
        public_url = resolve_public_url(root)
        if public_url is None:
            typer.echo(
                "error: no public base URL — set LOOPY_PUBLIC_URL in loopy.env "
                "(`loopy init` prompts for it), or run `loopy deploy bootstrap` to provision a "
                "host that writes it for you; or pass --url.",
                err=True,
            )
            raise typer.Exit(code=1)

    events = github_hook_events(project.sensors)
    verb = "checking" if check else "registering"
    typer.echo(
        f"{verb} GitHub webhooks → {public_url}{GITHUB_HOOK_PATH} "
        f"(events: {', '.join(events)})"
    )
    try:
        report = sync_github_webhooks(
            root, repos=repos, events=events, public_url=public_url, check=check
        )
    except MissingCredentials as exc:
        typer.echo(
            f"error: no GitHub App configured ({exc}) — run `loopy auth github` first.",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    needs_action = render_sync_report(report)
    if check and needs_action:
        typer.echo("  → run `loopy webhooks github` to fix the above")
    raise typer.Exit(code=1 if needs_action else 0)


@webhooks_app.command("list")
def list_(
    root: Path = typer.Option(
        Path("."), "--root", help="Project root (registry.yml + loopy.env)."
    ),
) -> None:
    """List every webhook endpoint this project serves, with its public delivery URL.

    Compiles the project and prints each webhook path with the sensors behind it
    (built-in and custom) and — when LOOPY_PUBLIC_URL is set — the full public delivery
    URL to paste into the source service's settings. Offline: no GitHub calls; for the
    live view of what GitHub actually has registered, use `loopy webhooks github --check`.
    """
    project = _compile_or_exit(root)
    paths: dict[str, list] = {}
    for sensor in project.sensors:
        if sensor.trigger.kind == "webhook" and sensor.trigger.path:
            paths.setdefault(sensor.trigger.path, []).append(sensor)
    if not paths:
        typer.echo("no webhook sensors — nothing listens over HTTP (poll/cron only)")
        return

    base = resolve_public_url(root)
    typer.echo()
    for path in sorted(paths):
        if base:
            typer.echo(
                "  " + typer.style(f"{base}{path}", fg=typer.colors.BRIGHT_WHITE, bold=True)
            )
        else:
            typer.echo(
                "  "
                + typer.style(path, fg=typer.colors.BRIGHT_WHITE, bold=True)
                + typer.style(
                    "   (set LOOPY_PUBLIC_URL in loopy.env for the public URL)",
                    fg=typer.colors.BRIGHT_BLACK,
                )
            )
        for sensor in paths[path]:
            kind = "built-in" if sensor.source == "builtin" else "sensor"
            typer.echo(
                "      "
                + typer.style(f"{kind} ", fg=typer.colors.BRIGHT_BLACK)
                + typer.style(sensor.name, fg=typer.colors.GREEN)
                + typer.style(" → emits ", fg=typer.colors.BRIGHT_BLACK)
                + typer.style(sensor.emits, fg=typer.colors.MAGENTA)
            )
        if path == GITHUB_HOOK_PATH:
            typer.echo(
                typer.style(
                    "      register on GitHub: loopy webhooks github", fg=typer.colors.BRIGHT_BLACK
                )
            )
        typer.echo()


def registration_findings(project, root: Path, *, control_env) -> list[Finding]:  # noqa: ANN001
    """Doctor findings for GitHub webhook wiring: is GitHub actually pointing at us?

    The gap the other preflights can't see: a project can compile green, have real
    creds, and still never receive a `Github.*` event because nothing was ever
    registered on GitHub's side. Scoped to projects with `/hooks/github` sensors.
    Warn-only — a webhook may legitimately be registered by hand or out of band —
    and any API failure degrades to a single "couldn't verify" warning. No App or no
    repos means we can't look (manual registration is a fine setup), so no finding.
    """
    from loopy_runtime.scm.github_app import GitHubAppError, MissingCredentials

    uses_github = any(
        s.trigger.kind == "webhook" and s.trigger.path == GITHUB_HOOK_PATH
        for s in project.sensors
    )
    if not uses_github:
        return []

    public_url = (control_env.get("LOOPY_PUBLIC_URL") or "").strip().rstrip("/")
    if not public_url:
        return [
            Finding(
                "warn",
                "workflows listen for GitHub webhooks but LOOPY_PUBLIC_URL is not set — "
                "GitHub has nowhere to deliver",
                "set LOOPY_PUBLIC_URL in loopy.env, then run `loopy webhooks github`",
            )
        ]

    repos = _declared_repo_slugs(project.registry)
    if not repos:
        return []
    try:
        report = sync_github_webhooks(
            root,
            repos=repos,
            events=github_hook_events(project.sensors),
            public_url=public_url,
            check=True,
        )
    except MissingCredentials:
        return []  # no App — may be registered by hand; nothing to verify against
    except (GitHubAppError, OSError) as exc:
        return [
            Finding(
                "warn",
                f"couldn't verify GitHub webhook registration: {exc}",
                "run `loopy webhooks github --check`",
            )
        ]

    unwired = [r.repo for r in report.results if r.action in ("missing", "stale")]
    errored = [r for r in report.results if r.action == "error"]
    findings: list[Finding] = []
    if unwired:
        findings.append(
            Finding(
                "warn",
                f"no up-to-date webhook delivers to {public_url}{GITHUB_HOOK_PATH} "
                f"from {', '.join(unwired)}",
                "run `loopy webhooks github` to register them",
            )
        )
    if errored:
        findings.append(
            Finding(
                "warn",
                f"couldn't verify GitHub webhook registration for "
                f"{', '.join(r.repo for r in errored)}: {errored[0].detail}",
                "run `loopy webhooks github --check`",
            )
        )
    return findings


def deploy_webhook_lines(root: Path, public_url: str) -> list[str] | None:
    """Post-deploy summary text for GitHub webhook wiring, as value-column lines.

    The first line is the summary value (the caller prints it right after the
    `webhooks:` label); any further lines are continuations the caller indents to
    the same column. Returns None when the project has no `/hooks/github` sensors —
    the deploy has nothing to say about GitHub delivery.

    A live `--check` against GitHub distinguishes two states:
    - already wired (every declared repo delivers here): a one-line confirmation.
    - not wired, or we can't confirm (no App, no repos, API error): a short blurb
      that `loopy webhooks github` is the next step and what it does.

    Any failure to check degrades to the nudge — pointing at the next command is
    always safe, and the command itself is idempotent, so an over-eager nudge costs
    nothing. Deliberately never registers anything; that stays an explicit step.
    """
    from loopy_core.compile.pipeline import compile_project
    from loopy_runtime.scm.github_app import GitHubAppError, MissingCredentials

    project = compile_project(root).project
    if project is None:
        return None  # a broken project is surfaced elsewhere; nothing to add here
    uses_github = any(
        s.trigger.kind == "webhook" and s.trigger.path == GITHUB_HOOK_PATH
        for s in project.sensors
    )
    if not uses_github:
        return None

    delivery = f"{public_url.rstrip('/')}{GITHUB_HOOK_PATH}"
    nudge = [
        "not registered yet — a smart next step. Run:",
        "  loopy webhooks github",
        "It registers a webhook on your repo(s) so GitHub delivers pull request,",
        f"issue, and push events to {delivery}.",
        "Until then the engine is live but nothing triggers your workflows.",
    ]

    repos = _declared_repo_slugs(project.registry)
    if not repos:
        return nudge
    try:
        report = sync_github_webhooks(
            root,
            repos=repos,
            events=github_hook_events(project.sensors),
            public_url=public_url,
            check=True,
        )
    except (MissingCredentials, GitHubAppError, OSError):
        return nudge

    if report.results and all(r.action == "ok" for r in report.results):
        return [f"registered — GitHub delivers to {delivery}"]
    return nudge
