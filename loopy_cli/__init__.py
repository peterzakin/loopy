"""Loopy CLI — one binary over both halves.

    loopy compile   produce the manifest (frontend)
    loopy doctor    preflight a project for its first run (placeholders, repo, git auth)
    loopy run       start the server: host sensor webhooks; events drive workflow runs
    loopy trigger   fire one event at the manifest and run it to completion (for testing)
    loopy dockerfile  generate a version-pinned Dockerfile (+ .dockerignore) for a git-push deploy
    loopy env       print the production env block to paste into a platform's settings
    loopy admin     serve the read-only dashboard for a deploy target (local|byo|bootstrap)
    loopy demo      serve the dashboard against in-memory fake data (dev-only; safe to delete)
    loopy help      show this overview, or help for one command (`loopy help run`)
    loopy docs      print the authoring reference (or `deployment`/`errors`) as markdown

Heavy deps are imported lazily per command so `loopy compile` stays runtime-free.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import sys
from pathlib import Path

import typer

from loopy_cli.auth import auth_app
from loopy_cli.bootstrap import deploy_app
from loopy_cli.deploy_target import TARGET_BYO
from loopy_cli.integrations import integrations_command
from loopy_cli.webhooks import (
    normalize_public_url,
    registration_findings,
    webhooks_app,
)

app = typer.Typer(
    add_completion=False, help="Loopy: agent workflows that run when your data changes."
)

# `loopy auth ...` — onboarding for external creds (GitHub App manifest flow). The
# sub-app's heavy imports are deferred into its command bodies, so registering it
# here keeps `loopy compile` runtime-free.
app.add_typer(auth_app, name="auth")

# `loopy webhooks ...` — wire external services' webhooks to this project's sensors
# (`loopy webhooks github` registers repo webhooks; `loopy webhooks list` prints every
# endpoint's delivery URL). Same deferred-imports discipline as `auth`.
app.add_typer(webhooks_app, name="webhooks")

# `loopy integrations [name]` — inspect the built-in webhook providers (github, sentry):
# which are used, whether each signing secret is configured, and per-provider setup.
app.command(name="integrations")(integrations_command)

# `loopy deploy <target>` — provision hosting for the engine from an operator's cloud
# keys. One subcommand per deploy target: `loopy deploy bootstrap` is the provisioned
# starter stack (one CloudFormation stack; see docs/design/aws-deploy.md). boto3 is
# imported inside the command body, keeping this registration weightless.
app.add_typer(deploy_app, name="deploy")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Loopy: agent workflows that run when your data changes."""
    # A bare `loopy` (no subcommand) is a request for orientation, not a usage mistake — so it
    # exits 0, never Typer's "Missing command." (exit 2) or Click's `no_args_is_help` (also 2),
    # both of which read as failure to scripts. *What* it orients you toward depends on where
    # you are: inside a project (a `registry.yml` is present) it's "what should I do next here?"
    # — print the guided next-steps ladder; anywhere else it's "how do I start?" — point at the
    # one command that applies with no project yet (`loopy init`), not the full command overview.
    # The complete list still lives at `loopy --help` / `loopy help`.
    if ctx.invoked_subcommand is None:
        if (Path.cwd() / "registry.yml").is_file():
            _print_next_steps(Path.cwd())
        else:
            _print_getting_started()
        raise typer.Exit()


def _print_getting_started() -> None:
    """Bare `loopy` outside a project: point a newcomer at the one command that applies here.

    Almost every loopy command operates on a project — `compile`, `run`, `doctor`, `trigger`,
    `deploy`, `webhooks`, `auth`, `env`, `dockerfile` all read a `registry.yml` and do nothing
    useful (or error) without one. Dumping the full command overview at someone who has no
    project yet buries the only relevant next step under a dozen that aren't. So lead with
    `loopy init`, and name `loopy docs` / `loopy help` for anyone who wants the rest. The full
    list still lives at `loopy --help`; this is orientation, not a replacement for it.
    """
    typer.echo()
    typer.echo(
        "  "
        + typer.style("Loopy", bold=True)
        + _dim(": agent workflows that run when your data changes.")
    )
    typer.echo()
    typer.echo(
        "  "
        + typer.style("You're not in a Loopy project yet.", bold=True)
        + _dim(" To scaffold one:")
    )
    typer.echo(
        typer.style("    loopy init", fg=typer.colors.BRIGHT_WHITE)
        + _dim("   # create a new project (interactive)")
    )
    typer.echo()
    typer.echo(_dim("  Already have a project? cd into it and run `loopy` for its next steps."))
    typer.echo(
        "  "
        + typer.style("loopy docs", fg=typer.colors.BRIGHT_WHITE)
        + _dim("  the authoring reference")
        + _dim("    ")
        + typer.style("loopy help", fg=typer.colors.BRIGHT_WHITE)
        + _dim("  every command")
    )
    typer.echo()


def _print_next_steps(root: Path) -> None:
    """Bare `loopy` inside a project: compile locally, then print the guided next-steps ladder.

    Local-only and network-free so it stays instant: it compiles the project (pure) and reads
    the control-plane env, but never reaches GitHub. A project that doesn't compile can't be
    reasoned about, so it just points at `loopy compile`; otherwise it renders the onboarding
    actions from `doctor.next_actions`, or a "ready to run" line when nothing's left to wire.
    """
    from loopy_cli.doctor import next_actions
    from loopy_core.compile.pipeline import compile_project
    from loopy_runtime.secrets import _parse_dotenv, load_control_plane_env

    typer.echo()
    result = compile_project(root)
    if result.diagnostics.has_errors() or result.project is None:
        typer.echo("  " + typer.style("This project doesn't compile yet.", bold=True))
        typer.echo("  Start here:")
        typer.echo(
            typer.style("    loopy compile .", fg=typer.colors.BRIGHT_WHITE)
            + _dim("   # show the errors")
        )
        typer.echo(_dim("  Run `loopy help` for all commands."))
        typer.echo()
        return

    def read_env(rel: str) -> dict[str, str] | None:
        env_path = root / rel
        return _parse_dotenv(env_path.read_text()) if env_path.is_file() else None

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)  # real/platform env wins over loopy.env

    actions = next_actions(result.project, read_env=read_env, control_plane_env=merged)

    if actions:
        typer.echo(
            "  "
            + typer.style("Suggested next steps", bold=True)
            + _dim(" to get this project receiving events:")
        )
        for action in actions:
            typer.echo(typer.style(f"    {action.command}", fg=typer.colors.BRIGHT_WHITE))
            typer.echo(_dim(f"      {action.why}"))
    else:
        typer.echo(
            "  "
            + typer.style("✓  ready to run", fg=typer.colors.GREEN, bold=True)
            + _dim("   — nothing left to wire up")
        )

    typer.echo()
    typer.echo(
        "  "
        + typer.style("loopy doctor", fg=typer.colors.BRIGHT_WHITE)
        + _dim("  re-checks readiness (with the live GitHub checks this skips)")
    )
    typer.echo(
        "  "
        + typer.style("loopy run", fg=typer.colors.BRIGHT_WHITE)
        + _dim("     compiles + starts the engine")
    )
    typer.echo(_dim("  Run `loopy help` for all commands."))
    typer.echo()


@app.command()
def help(  # noqa: A001 - the command is literally named `help`; shadowing the builtin is intended
    ctx: typer.Context,
    command: list[str] | None = typer.Argument(
        None, help="Command to describe, e.g. `loopy help run` or `loopy help auth github`."
    ),
) -> None:
    """Show help for loopy, or for a specific command.

    `loopy help` prints the top-level overview (same as `loopy --help`); `loopy help <command>`
    prints that command's help (same as `loopy <command> --help`), walking sub-apps like
    `loopy help auth github`.
    """
    # The group context is the parent — `ctx` itself belongs to this `help` command. Rendering
    # the parent's help keeps the output identical to `loopy --help`, with no second source to
    # drift. Walking `get_command` down the tree reuses click's own resolution, so a new command
    # or sub-app shows up here automatically.
    #
    # Duck-typed on purpose: Typer renders through a vendored click (`typer._click`), so its
    # groups are NOT instances of the top-level `click.Group` and an isinstance check would
    # wrongly reject them. We test for `get_command` (only groups have it) and build each
    # sub-context from the parent context's own class, staying within whichever click Typer uses.
    group_ctx = ctx.parent
    if not command:
        typer.echo(group_ctx.get_help())
        return

    context_cls = type(group_ctx)
    cmd = group_ctx.command
    cmd_ctx = group_ctx
    path = "loopy"
    for token in command:
        get_command = getattr(cmd, "get_command", None)
        if get_command is None:  # a leaf command — nothing to descend into
            typer.echo(f"error: '{path}' has no subcommands", err=True)
            raise typer.Exit(code=1)
        sub = get_command(cmd_ctx, token)
        if sub is None:
            typer.echo(
                f"error: unknown command '{token}'. Run `loopy help` for the command list.",
                err=True,
            )
            raise typer.Exit(code=1)
        cmd_ctx = context_cls(sub, info_name=token, parent=cmd_ctx)
        cmd = sub
        path = f"{path} {token}"
    typer.echo(cmd.get_help(cmd_ctx))


# The docs shipped inside the package, printed as plain markdown. This exists for coding
# agents (and offline humans): docs that travel with the install are always version-matched
# and need no URL — an agent can run `loopy docs` and get the whole authoring model as
# context. Topics map to files in docs_md/, except `errors`, which renders the live
# diagnostic catalog from loopy_core so it can never drift from the compiler.
_DOCS_DIR = Path(__file__).resolve().parent / "docs_md"
_DOCS_TOPICS = ("authoring", "deployment", "errors")


def _render_error_catalog() -> str:
    """The LOOPY-Exxx/Wxxx catalog as markdown, straight from the compiler's own table."""
    from loopy_core.compile.codes import DESCRIPTIONS

    lines = [
        "# Loopy diagnostic codes",
        "",
        "Emitted by `loopy compile` as `<severity> <code> <file>:<line>:<col>: <message>`,",
        "often with a `hint:` line. Codes are stable and never renumbered.",
        "",
        "| Code | Meaning |",
        "|---|---|",
    ]
    lines += [f"| `{code}` | {desc} |" for code, desc in sorted(DESCRIPTIONS.items())]
    return "\n".join(lines)


@app.command()
def docs(
    topic: str = typer.Argument(
        "authoring",
        help="What to print: `authoring` (the full reference), `deployment` (hosting the "
        "control plane + admin auth), or `errors`.",
    ),
) -> None:
    """Print loopy's documentation as markdown, for agents and offline reading.

    `loopy docs` prints the full authoring reference (workflows, steps, registry, sensors,
    secrets — everything needed to write a project); `loopy docs deployment` prints the
    hosting contract (env vars, $PORT, TLS at the ingress, admin-dashboard auth); `loopy
    docs errors` prints the diagnostic-code catalog. Output is plain markdown on stdout, so
    it pipes cleanly into a pager, a file, or an agent's context.
    """
    if topic == "errors":
        typer.echo(_render_error_catalog())
        return
    doc = _DOCS_DIR / f"{topic}.md"
    if not doc.is_file():
        known = ", ".join(_DOCS_TOPICS)
        typer.echo(f"error: unknown docs topic '{topic}'. Topics: {known}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(doc.read_text())


def _enable_progress_logging() -> None:
    """Surface the runtime's INFO-level lifecycle breadcrumbs (e.g. the Daytona build/boot
    progress) on stderr.

    The CLI installs no root logging config, so these records are otherwise swallowed at
    Python's default WARNING threshold — which is exactly why a `--sandbox daytona` build can
    look hung for minutes. Scoped to the `loopy_runtime` namespace (not the root logger) so
    third-party SDK chatter (httpx, the Daytona client) stays quiet, and to stderr so it never
    pollutes the structured run record on stdout. Idempotent — safe to call per command."""
    logger = logging.getLogger("loopy_runtime")
    if any(getattr(h, "_loopy_progress", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._loopy_progress = True  # type: ignore[attr-defined]  # tag for the idempotency guard
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _resolve_dashboard_port(host: str, preferred: int, *, attempts: int = 20) -> int:
    """Return a bindable port at or after `preferred` on `host`.

    The dashboard's default port is routinely already taken — a second `loopy admin`,
    a `loopy demo` left running, an unrelated service. Rather than let uvicorn dump a raw
    `address already in use` traceback, probe for a free port in a small window and announce
    the fallback. Raises `typer.Exit` with a clear message (not a traceback) if the whole
    window is busy, pointing the user at `--port`."""
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Match uvicorn's own SO_REUSEADDR so a successful probe predicts a successful serve.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError as exc:
                if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue  # busy or privileged — try the next one
                raise
            if candidate != preferred:
                typer.echo(
                    f"note: {host}:{preferred} is in use — serving on {candidate} instead "
                    "(pass --port to choose another)",
                    err=True,
                )
            return candidate
    typer.echo(
        f"error: no free port in {preferred}–{preferred + attempts - 1} on {host}. "
        "Stop the existing dashboard or pass --port.",
        err=True,
    )
    raise typer.Exit(code=1)


def _serve_dashboard(config) -> None:
    """Run a uvicorn server, turning a late bind failure into a clean error.

    `_resolve_dashboard_port` removes the common case, but a port can still be claimed in the
    race between probe and serve. Catch that here so the user gets one tidy line instead of a
    stack trace."""
    import asyncio

    import uvicorn

    try:
        asyncio.run(uvicorn.Server(config).serve())
    except OSError as exc:
        typer.echo(
            f"error: could not serve on {config.host}:{config.port} ({exc}).",
            err=True,
        )
        raise typer.Exit(code=1) from exc


def _dim(text: str) -> str:
    return typer.style(text, fg=typer.colors.BRIGHT_BLACK)


def _workflow_generations(wf) -> list[list[str]]:  # noqa: ANN001
    """Group a workflow's steps into dependency layers (topological generations).

    Steps in the same generation have no ordering between them — they run in parallel —
    so the diagram renders them as a fork. `after` holds local step names; the entry step
    has none, so it leads the first generation.
    """
    steps = wf.steps
    deps = {name: set(step.after) for name, step in steps.items()}
    placed: set[str] = set()
    remaining = set(steps)
    generations: list[list[str]] = []
    while remaining:
        ready = [n for n in steps if n in remaining and deps[n] <= placed]
        if not ready:  # a cycle (caught upstream) or a dangling ref — don't loop forever
            ready = [n for n in steps if n in remaining]
        generations.append(ready)
        placed |= set(ready)
        remaining -= set(ready)
    return generations


def _print_workflow_diagram(name: str, wf) -> None:  # noqa: ANN001
    """Render one workflow as a top-to-bottom flow diagram: trigger → steps → emitted events."""
    entry = wf.steps.get(wf.entry) if wf.entry else None
    trigger = entry.trigger if entry else None
    if trigger and trigger.kind == "event":
        icon, source = "⚡", f"on event {trigger.event}"
    elif trigger and trigger.kind == "cron":
        icon, source = "⏰", f"on cron({trigger.expr})"
    else:
        icon, source = "❓", "no trigger"

    typer.echo()
    typer.echo(f"  {icon}  " + typer.style(name, fg=typer.colors.BRIGHT_WHITE, bold=True))

    indent = "      "
    generations = _workflow_generations(wf)
    forked = any(len(gen) > 1 for gen in generations)
    glyph_w = 3 if forked else 1  # "├─●" / "└─●" / "  ●"  vs  "●"
    name_w = max((len(n) for gen in generations for n in gen), default=0)
    label_w = glyph_w + 1 + name_w  # glyph + space + name

    sep = typer.style("  ·  ", fg=typer.colors.BRIGHT_BLACK)

    # The trigger is the source node the flow descends from.
    typer.echo(indent + _dim("◇ ") + typer.style(source, fg=typer.colors.YELLOW))
    if generations:
        typer.echo(indent + _dim("│"))
        typer.echo(indent + _dim("▼"))

    for gi, gen in enumerate(generations):
        multi = len(gen) > 1
        for ni, step_name in enumerate(gen):
            step = wf.steps[step_name]
            if multi:
                glyph = "└─●" if ni == len(gen) - 1 else "├─●"
            else:
                glyph = "  ●" if forked else "●"
            raw = f"{glyph} {step_name}"
            pad = " " * max(2, label_w + 2 - len(raw))
            line = indent + _dim(glyph) + " " + typer.style(step_name, fg=typer.colors.GREEN) + pad
            meta = []
            if step.agent:
                meta.append("🤖 " + typer.style(step.agent, fg=typer.colors.BLUE))
            if step.emits:
                meta.append("📡 " + typer.style(", ".join(step.emits), fg=typer.colors.MAGENTA))
            # `after` is implied by the arrows for a simple chain; only call it out for joins.
            if len(step.after) > 1:
                meta.append(
                    _dim("after ") + typer.style(", ".join(step.after), fg=typer.colors.CYAN)
                )
            if meta:
                line += sep.join(meta)
            typer.echo(line)
        if gi != len(generations) - 1:
            typer.echo(indent + _dim("│"))
            typer.echo(indent + _dim("▼"))


def _print_wiring(project) -> None:  # noqa: ANN001
    """Show how workflows chain together: which producer emits each event, and who consumes it."""

    def names(ids: list[str]) -> str:
        # Consumers are "<workflow>/<step>"; producers may be bare sensor names. Collapse to
        # the originating workflow/sensor and de-dup while preserving order.
        out: list[str] = []
        for i in ids:
            head = i.split("/")[0]
            if head not in out:
                out.append(head)
        return ", ".join(out) if out else "—"

    events = project.lineage.events
    if not events:
        return
    rows = [(names(lin.producers), ev, names(lin.consumers)) for ev, lin in sorted(events.items())]

    typer.echo()
    typer.echo(typer.style("  🔗  Event wiring", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))
    prod_w = max(len(r[0]) for r in rows)
    ev_w = max(len(r[1]) for r in rows)
    arrow = _dim("  ─▶  ")
    for prod, ev, cons in rows:
        typer.echo(
            "      "
            + typer.style(prod.ljust(prod_w), fg=typer.colors.BLUE)
            + arrow
            + typer.style(ev.ljust(ev_w), fg=typer.colors.MAGENTA)
            + arrow
            + typer.style(cons, fg=typer.colors.GREEN)
        )


def _print_workflows(project) -> None:  # noqa: ANN001 - loopy_core.compile.model.Project
    """Print a flow diagram of every compiled workflow, then the event wiring between them."""
    workflows = project.workflows
    count = len(workflows)
    plural = "workflow" if count == 1 else "workflows"
    typer.echo()
    summary = f"  🔁  Loopy compiled {count} {plural}"
    typer.echo(typer.style(summary, fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))

    for name, wf in sorted(workflows.items()):
        _print_workflow_diagram(name, wf)

    _print_wiring(project)
    typer.echo()


@app.command()
def init(
    name: str | None = typer.Argument(
        None, help="Project name — also the new directory's name. Prompted for if omitted."
    ),
    directory: Path = typer.Option(
        Path("."), "--dir", help="Parent directory to create the project under (default: cwd)."
    ),
) -> None:
    """Scaffold a new Loopy project: registry, a runnable starter workflow, and an env file.

    A short wizard drives the whole thing — it offers to close the gaps the scaffold leaves
    on purpose: recording the public base URL webhooks are delivered at, wiring git auth
    (and, with both in place, registering GitHub webhooks), reusing an `ANTHROPIC_API_KEY`
    already in your environment, and recording a Redis connection string if you want the
    networked event bus — then reports whatever's still missing (the same checks as
    `loopy doctor`). There is no non-interactive mode: setup is a conversation with a human
    (git auth alone needs a browser), so `init` requires a terminal.

    The default workflow is a PR reviewer: a pull request is opened → an agent reviews the diff
    and posts review comments. Git auth is its gate: loopy is built around agents that work on
    code, so without git auth wired (or, past that, without a repo to act on) that loop can't
    run. `init` still scaffolds it, but disabled (as `code-review.md.disabled`), alongside a
    trimmed-down registry.yml, and points at wiring GitHub — then renaming the file — as the
    next step.
    """
    from loopy_cli.scaffold import InvalidProjectName, scaffold_project, validate_project_name

    # The wizard is the command — without a human on a terminal there's nobody to answer the
    # repo question (and `loopy auth github` needs a browser), so refuse rather than guess.
    if not sys.stdin.isatty():
        typer.echo(
            "error: loopy init is interactive and needs a terminal. "
            "Ask a human to run it — there is no headless mode.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not name:
        name = typer.prompt("Project name")
    try:
        name = validate_project_name(name)
    except InvalidProjectName as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    target = (directory / name).resolve()

    # The deploy target comes first: it decides where the public webhook URL comes from,
    # the one setup step that can't follow a single order (a provisioned host mints its URL
    # only at deploy time). Bring-your-own means we can record the URL now; the bootstrap
    # target means `loopy deploy bootstrap` writes it back later, so there's nothing to ask.
    deploy_target = _choose_deploy_target(target)
    if deploy_target == TARGET_BYO:
        _offer_public_webhook_url(target)

    # Wire git auth *before* asking which repo(s) the agent works on. `loopy auth github` creates
    # the App and installs it on the repos it may touch, so it reads better to authenticate and
    # install first, then name the repo(s) with that install fresh in mind — rather than naming
    # repos into a registry before any auth exists. Both steps write loopy.env into `target`;
    # `scaffold_project` (run below) preserves those values under its own template.
    github_authed = _offer_github_auth(target)

    # Git auth is the gate for the default (PR-review) workflow. Loopy is built around agents that
    # work on a checkout, so without git auth wired that loop can't run — we skip straight to the
    # minimal scaffold (which ships the workflow disabled) rather than ask which repo(s) to name
    # into a project that can't reach them. With auth in place, ask which repo(s) the workflow
    # should act on; blank is allowed but strongly discouraged, and confirmed explicitly, and we
    # never fall back to a placeholder repo.
    repos = _prompt_for_repos() if github_authed else []

    try:
        created = scaffold_project(target, name, repos=repos)
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo()
    typer.echo(
        typer.style(f"  📦  Created Loopy project '{name}'", fg=typer.colors.CYAN, bold=True)
    )
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))
    for rel in created:
        typer.echo("      " + typer.style(f"{name}/{rel.as_posix()}", fg=typer.colors.GREEN))
    typer.echo()

    # Offer to close the remaining gaps the scaffold leaves on purpose before reporting what's left.
    # The admin token needs no prompt — it's pure entropy, so `init` just mints and records it.
    _write_admin_token(target)
    _offer_ambient_anthropic_key(target)
    _offer_ambient_daytona_creds(target)
    _offer_redis_bus(target)
    # Webhook registration is deliberately *not* offered here — it's one explicit
    # `loopy webhooks github` step, surfaced as a next step below once a public URL exists
    # (on the bootstrap target that URL doesn't arrive until `loopy deploy bootstrap`).

    # A repo-less scaffold ships the default workflow disabled — nothing runs yet. Repeat the
    # warning the user already confirmed past, and point at the way out.
    if not repos:
        _note_minimal_mode()

    # A clean compile is *not* a runnable project: the scaffold ships placeholders on purpose
    # (a fake API key, maybe no git auth). Run the same checks as `loopy doctor` so the user
    # sees the *actual* remaining gaps — anything resolved above is already gone.
    _report_remaining_setup(target, name, deploy_target)

    # The built-in `Github.*` sensors only fire once GitHub can deliver to the engine, and
    # that hinges on one thing (a public URL) with a different next step on each side. Spell
    # it out rather than leaving it implicit in the commands list above.
    _explain_github_webhooks(target, deploy_target)


def _prompt_for_repos() -> list[str]:
    """Ask which repo(s) the agent should work on, strongly discouraging the repo-less path.

    A repo is what the default `review` workflow checks out to review an opened PR and post
    comments on — loopy is built around agents that work on code, so without one the default
    loop ships disabled. Blank is therefore never a silent default: it takes an explicit
    confirmation (declining re-asks for repos), and we never fall back to an unpushable
    placeholder.
    """
    while True:
        raw = typer.prompt(
            "  Which repo(s) should the agent work on? (owner/repo, comma-separated)",
            default="",
            show_default=False,
        )
        repos = [r.strip() for r in raw.split(",") if r.strip()]
        if repos:
            return repos
        typer.echo(
            "  "
            + typer.style("⚠", fg=typer.colors.YELLOW)
            + " Loopy is built around agents that work on repos (clone, edit, open a PR)."
        )
        typer.echo(
            typer.style(
                "    Without one the default PR-review workflow ships disabled — "
                "there is nothing useful to run until GitHub access is wired.",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
        if typer.confirm("  Continue without a repo anyway?", default=False):
            return []


def _note_minimal_mode() -> None:
    """A minimal scaffold (no git auth, or no repo) ships the default workflow disabled.

    Warn, and name the way out."""
    typer.echo(
        "  "
        + typer.style("⚠", fg=typer.colors.YELLOW)
        + " Minimal scaffold — the default PR-review workflow ships disabled (no GitHub access)."
    )
    typer.echo(
        typer.style(
            "    To enable it: run `loopy auth github`, add repo(s) to sandboxes.BaseSandbox.repos,"
            " then rename workflows/review/code-review.md.disabled to drop `.disabled`.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    typer.echo()


def _offer_ambient_anthropic_key(target: Path) -> None:
    """If a real `ANTHROPIC_API_KEY` is in the environment, offer to write it into the env_file.

    The scaffold ships a `sk-ant-...` placeholder that compiles green but fails the first run.
    Most people trying loopy already have a key exported, so reuse it on the spot rather than
    making them hand-edit `secrets/base.env`. Skips silently when no usable key is present.
    """
    from loopy_cli.doctor import PLACEHOLDER_ANTHROPIC_KEY

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key == PLACEHOLDER_ANTHROPIC_KEY:
        return

    env_path = target / "secrets" / "base.env"
    placeholder_line = f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"
    if not env_path.is_file() or placeholder_line not in env_path.read_text():
        return  # nothing to replace (already set, or layout changed) — don't guess

    masked = f"{key[:7]}…{key[-4:]}" if len(key) > 12 else "the value"
    if not typer.confirm(
        f"  Found ANTHROPIC_API_KEY in your environment ({masked}). "
        f"Write it into secrets/base.env?",
        default=True,
    ):
        return

    text = env_path.read_text().replace(placeholder_line, f"ANTHROPIC_API_KEY={key}")
    env_path.write_text(text)
    typer.echo(
        "  "
        + typer.style("✓", fg=typer.colors.GREEN)
        + " wrote ANTHROPIC_API_KEY to secrets/base.env"
    )
    typer.echo()


def _offer_ambient_daytona_creds(target: Path) -> None:
    """If `DAYTONA_API_KEY` is in the environment, offer to write it into the control-plane env.

    The default scaffold's sandbox is `provider: daytona`, which needs `DAYTONA_API_KEY` in
    `loopy.env` to run — the scaffold ships it as a commented placeholder. Most people trying
    loopy with Daytona already have the key exported, so reuse it on the spot rather than making
    them hand-edit `loopy.env`. `DAYTONA_API_URL` (an override for non-prod Daytona deployments;
    the SDK defaults to prod) is only carried along when the key is present (a URL alone can't
    authenticate). Skips silently when no key is in the environment, or when `loopy.env` already
    has one (don't clobber).
    """
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    key = os.environ.get("DAYTONA_API_KEY", "").strip()
    if not key:
        return

    # Already configured (e.g. re-init over an existing tree) — nothing to offer, don't clobber.
    if load_control_plane_env(target).get("DAYTONA_API_KEY"):
        return

    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "the value"
    if not typer.confirm(
        f"  Found DAYTONA_API_KEY in your environment ({masked}). Write it into loopy.env?",
        default=True,
    ):
        return

    updates = {"DAYTONA_API_KEY": key}
    url = os.environ.get("DAYTONA_API_URL", "").strip()
    if url:
        updates["DAYTONA_API_URL"] = url

    write_control_plane_env(target, updates)
    wrote = " + ".join(updates)
    typer.echo("  " + typer.style("✓", fg=typer.colors.GREEN) + f" wrote {wrote} to loopy.env")
    typer.echo()


def _offer_redis_bus(target: Path) -> None:
    """Ask whether to use Redis as the event bus, and if so record the connection string.

    The default bus is the in-process backend — right for a first local run, no external service.
    Redis is the networked mode (decoupled Runtime workers consuming a shared stream, surviving
    restarts). Opting in writes the connection string into `loopy.env` as `REDIS_URL` (replacing
    its commented stub in place); `loopy run` auto-selects the Redis bus whenever `REDIS_URL` is
    set, so no flag is needed (and `--bus inproc` still forces in-process). Skips
    silently when `loopy.env` already has a `REDIS_URL` (e.g. re-init) so we never clobber one.
    """
    from loopy_runtime.config import DEFAULT_REDIS_URL
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    # Already configured (e.g. re-init over an existing tree) — nothing to offer, don't clobber.
    if load_control_plane_env(target).get("REDIS_URL"):
        return

    if not typer.confirm(
        "  Use Redis as the event bus? (default: in-process — single node, no external service)",
        default=False,
    ):
        return

    url = typer.prompt("  Redis connection string", default=DEFAULT_REDIS_URL).strip()
    if not url:
        url = DEFAULT_REDIS_URL

    write_control_plane_env(target, {"REDIS_URL": url})
    typer.echo(
        "  "
        + typer.style("✓", fg=typer.colors.GREEN)
        + " wrote REDIS_URL to loopy.env — `loopy run` will use the Redis bus automatically"
    )
    typer.echo()


def _write_admin_token(target: Path) -> None:
    """Mint the admin-dashboard bearer token and write it into `loopy.env`.

    Unlike the other setup steps this needs no prompt: the token is pure entropy from the
    CSPRNG (no external account to reach, no browser), so there is nothing to ask — `init`
    just mints one and records it, replacing the scaffold's commented `# LOOPY_ADMIN_TOKEN=`
    stub in place. It gates the /admin dashboard on any non-loopback bind; local (loopback)
    dev ignores it, so writing it now is harmless and means the operator never has to hand-run
    a `secrets.token_urlsafe` incantation and get the `loopy_sk_` prefix right. Skips silently
    when `loopy.env` already has one (e.g. re-init) so we never rotate it out from under a
    running deployment.
    """
    from loopy_runtime.dashboard.auth import generate_admin_token
    from loopy_runtime.secrets import (
        ADMIN_TOKEN_ENV,
        load_control_plane_env,
        write_control_plane_env,
    )

    # Already configured (e.g. re-init over an existing tree) — never clobber a live token.
    if load_control_plane_env(target).get(ADMIN_TOKEN_ENV):
        return

    write_control_plane_env(target, {ADMIN_TOKEN_ENV: generate_admin_token()})
    typer.echo(
        "  "
        + typer.style("✓", fg=typer.colors.GREEN)
        + f" minted {ADMIN_TOKEN_ENV} in loopy.env — gates the /admin dashboard on a "
        "non-loopback bind"
    )
    typer.echo(
        typer.style(
            "    On deploy, copy the same value into the platform environment "
            "(Fly/Render/Railway secrets).",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    typer.echo()


def _choose_deploy_target(target: Path) -> str:
    """Ask how the engine will be hosted, and return the choice (in-memory only).

    This is the fork the rest of onboarding hinges on, because the public webhook URL can't
    be collected in a single linear order — a provisioned host doesn't mint its URL until
    deploy time:

    - `byo` (bring-your-own): you have a URL now (a domain, a dev tunnel, or a platform
      hostname like Render's). `init` prompts for it next and webhook delivery is wired
      right after.
    - `bootstrap` (the provisioned starter stack): `loopy deploy bootstrap` stands up the
      host and its `*.cloudfront.net` URL from your AWS credentials, then writes
      `LOOPY_PUBLIC_URL` back for you. `init` skips the URL prompt entirely.

    The answer steers only the rest of *this* `init` run (whether to prompt for the URL, how
    to order the next-steps list); it is not persisted, so re-init simply asks again. Anything
    but an explicit "2" falls back to bring-your-own (the safe default: it just prompts for a
    URL, and blank is a first-class answer there).
    """
    from loopy_cli.deploy_target import TARGET_BOOTSTRAP

    typer.echo("  How will you host the engine?")
    typer.echo(
        "    1) I'll provide the public URL — a domain, a dev tunnel, or a platform like Render"
    )
    typer.echo(
        "    2) Provision a starter stack for me — `loopy deploy bootstrap` stands up the "
        "host on AWS and mints the URL"
    )
    choice = typer.prompt("  Choose 1 or 2", default="1").strip()
    chosen = TARGET_BOOTSTRAP if choice == "2" else TARGET_BYO

    if chosen == TARGET_BOOTSTRAP:
        typer.echo(
            "  "
            + typer.style("✓", fg=typer.colors.GREEN)
            + " bootstrap target — `loopy deploy bootstrap` sets LOOPY_PUBLIC_URL for you "
            "at deploy."
        )
    typer.echo()
    return chosen


def _offer_public_webhook_url(target: Path) -> None:
    """Ask for the public base URL external services deliver webhooks to.

    Webhook sensors bind locally (`loopy run --host/--port`), but the services that call them
    (GitHub, Sentry, …) need a public URL — a deployed host or a dev tunnel (ngrok,
    cloudflared). Recording it as `LOOPY_PUBLIC_URL` in `loopy.env` gives every webhook one
    base: a sensor's delivery URL is `LOOPY_PUBLIC_URL + its path` (the built-in GitHub
    sensor: `<base>/hooks/github`), and `loopy run` prints the full delivery URLs at startup
    so they can be pasted into each source's webhook settings. Blank is a first-class answer
    (nothing external delivers webhooks yet — sensors still serve locally); skips silently
    when `loopy.env` already has one (e.g. re-init) so we never clobber it.
    """
    from loopy_runtime.secrets import load_control_plane_env, write_control_plane_env

    # Already configured (e.g. re-init over an existing tree) — nothing to offer, don't clobber.
    if load_control_plane_env(target).get("LOOPY_PUBLIC_URL"):
        return

    while True:
        raw = typer.prompt(
            "  Public base URL where webhooks are delivered? (e.g. https://loopy.example.com "
            "or a tunnel URL; blank = none yet)",
            default="",
            show_default=False,
        ).strip()
        if not raw:
            return
        try:
            url = normalize_public_url(raw)
        except ValueError as exc:
            typer.echo("  " + typer.style(f"✗ {exc}", fg=typer.colors.RED))
            continue
        break

    write_control_plane_env(target, {"LOOPY_PUBLIC_URL": url})
    typer.echo(
        "  "
        + typer.style("✓", fg=typer.colors.GREEN)
        + " wrote LOOPY_PUBLIC_URL to loopy.env — webhook sensors receive deliveries at "
        f"{url}/hooks/<name> (GitHub: {url}/hooks/github)"
    )
    typer.echo()


def _offer_github_auth(target: Path) -> bool:
    """Offer to wire git auth now by running the `loopy auth github` manifest flow in-process.

    Returns whether git auth is configured once the offer is done — the caller uses this to
    decide between the coding starter (auth present) and the minimal registry (auth absent).
    Declining just leaves the step for later — `_report_remaining_setup` will flag it. A failed
    or aborted auth run is caught so it never takes the whole `init` down with it.
    """
    from loopy_runtime.secrets import load_control_plane_env

    # Already configured (e.g. re-init over an existing tree) — nothing to offer.
    if load_control_plane_env(target).get("GITHUB_APP_ID"):
        return True

    if not typer.confirm(
        "  Wire git auth now? Runs `loopy auth github` (creates a GitHub App, opens a browser)",
        default=True,
    ):
        return False

    from loopy_cli.auth import run_github_auth

    try:
        run_github_auth(root=target)
    except typer.Exit:
        typer.echo("  (skipped — git auth not completed; run `loopy auth github` later)")
        return False
    except Exception as exc:  # noqa: BLE001 - never let onboarding crash the scaffold
        typer.echo(f"  (git auth didn't complete: {exc} — run `loopy auth github` later)")
        return False

    # Confirm the App creds actually landed rather than assuming the flow succeeded.
    return bool(load_control_plane_env(target).get("GITHUB_APP_ID"))


def _report_remaining_setup(target: Path, name: str, deploy_target: str) -> None:
    """Compile the fresh project and print the gaps still blocking a first run (doctor's checks).

    Replaces the old static checklist: because the wizard may have already set the key or wired
    auth, we report the *actual* remaining findings instead of a fixed list the user has to
    re-verify by hand. Then we point at the next commands, ordered for the chosen deploy
    target — the bootstrap target deploys before it can register webhooks, bring-your-own can
    register straight away.
    """
    from loopy_core.compile.pipeline import compile_project

    result = compile_project(target)
    findings = (
        _diagnose_runnability(target, result.project)
        if not result.diagnostics.has_errors() and result.project is not None
        else None
    )

    if findings:
        count = len(findings)
        noun = "thing" if count == 1 else "things"
        typer.echo(f"  Before your first run — {count} {noun} left:")
        _render_findings(findings)
        typer.echo()
    elif findings == []:
        typer.echo(
            "  "
            + typer.style("✓  ready to run", fg=typer.colors.GREEN, bold=True)
            + typer.style(
                "   — nothing blocking a first run",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
        typer.echo()

    from loopy_cli.deploy_target import TARGET_BOOTSTRAP

    typer.echo("  Then:")
    typer.echo(typer.style(f"    cd {name}", fg=typer.colors.BRIGHT_WHITE))
    typer.echo(
        typer.style("    loopy doctor", fg=typer.colors.BRIGHT_WHITE)
        + typer.style("          # re-check the above any time", fg=typer.colors.BRIGHT_BLACK)
    )
    if deploy_target == TARGET_BOOTSTRAP:
        typer.echo(
            typer.style("    loopy deploy bootstrap", fg=typer.colors.BRIGHT_WHITE)
            + typer.style(
                "  # provision the host; sets LOOPY_PUBLIC_URL for you",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
        typer.echo(
            typer.style("    loopy webhooks github", fg=typer.colors.BRIGHT_WHITE)
            + typer.style(
                "  # register GitHub delivery, after deploy", fg=typer.colors.BRIGHT_BLACK
            )
        )
    else:
        typer.echo(
            typer.style("    loopy webhooks github", fg=typer.colors.BRIGHT_WHITE)
            + typer.style(
                "  # register GitHub delivery (needs LOOPY_PUBLIC_URL)",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
        typer.echo(
            typer.style("    loopy run", fg=typer.colors.BRIGHT_WHITE)
            + typer.style(
                "             # compiles + starts the engine", fg=typer.colors.BRIGHT_BLACK
            )
        )
    typer.echo()


def _explain_github_webhooks(target: Path, deploy_target: str) -> None:
    """Explain how to turn on the built-in `Github.*` events, branching on the public URL.

    The GitHub sensors deliver over *repo* webhooks (not an App webhook), registered once by
    `loopy webhooks github` — but GitHub can only deliver to a public HTTPS URL, so the whole
    step hinges on whether `LOOPY_PUBLIC_URL` is set yet. That single fork has a genuinely
    different next command on each side, so we say it plainly rather than leaving the user to
    infer it from the commands list:

    - URL already recorded: `loopy webhooks github` works right now — that's the next command.
    - No URL yet: get one first. The bootstrap target mints it at `loopy deploy bootstrap`;
      bring-your-own means recording a domain or dev-tunnel URL (`loopy init` re-prompts, or
      set `LOOPY_PUBLIC_URL` in loopy.env by hand). Then `loopy webhooks github`.
    """
    from loopy_cli.deploy_target import TARGET_BOOTSTRAP
    from loopy_runtime.secrets import load_control_plane_env

    public_url = load_control_plane_env(target).get("LOOPY_PUBLIC_URL")

    typer.echo(
        typer.style("  Native GitHub events", bold=True)
        + typer.style(
            "  (the built-in Github.* sensors — PRs, issues, pushes)",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    typer.echo(
        typer.style(
            "    These fire only once GitHub delivers repo webhooks to the engine. "
            "`loopy webhooks github`",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    typer.echo(
        typer.style(
            "    registers that delivery; it needs a public HTTPS URL (LOOPY_PUBLIC_URL) to "
            "deliver to.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )

    if public_url:
        typer.echo(
            "    "
            + typer.style("✓", fg=typer.colors.GREEN)
            + f" LOOPY_PUBLIC_URL is set ({public_url}) — GitHub delivers to "
            + typer.style(f"{public_url}/hooks/github", fg=typer.colors.BRIGHT_BLACK)
            + "."
        )
        typer.echo("    Next:")
        typer.echo(
            typer.style("      loopy webhooks github", fg=typer.colors.BRIGHT_WHITE)
            + typer.style("  # register delivery now", fg=typer.colors.BRIGHT_BLACK)
        )
    else:
        typer.echo(
            "    "
            + typer.style("⚠", fg=typer.colors.YELLOW)
            + " No LOOPY_PUBLIC_URL yet — set one before registering delivery:"
        )
        if deploy_target == TARGET_BOOTSTRAP:
            typer.echo(
                typer.style("      loopy deploy bootstrap", fg=typer.colors.BRIGHT_WHITE)
                + typer.style(
                    "  # provisions the host and writes LOOPY_PUBLIC_URL for you",
                    fg=typer.colors.BRIGHT_BLACK,
                )
            )
        else:
            typer.echo(
                typer.style(
                    "      • bootstrap it: ", fg=typer.colors.BRIGHT_BLACK
                )
                + typer.style("loopy deploy bootstrap", fg=typer.colors.BRIGHT_WHITE)
                + typer.style(
                    "  (provisions a host and writes LOOPY_PUBLIC_URL)",
                    fg=typer.colors.BRIGHT_BLACK,
                )
            )
            typer.echo(
                typer.style(
                    "      • or set it yourself: re-run ", fg=typer.colors.BRIGHT_BLACK
                )
                + typer.style("loopy init", fg=typer.colors.BRIGHT_WHITE)
                + typer.style(
                    ", or add LOOPY_PUBLIC_URL (a domain or dev-tunnel URL) to loopy.env",
                    fg=typer.colors.BRIGHT_BLACK,
                )
            )
        typer.echo(
            typer.style("      then ", fg=typer.colors.BRIGHT_BLACK)
            + typer.style("loopy webhooks github", fg=typer.colors.BRIGHT_WHITE)
            + typer.style("  # register delivery", fg=typer.colors.BRIGHT_BLACK)
        )
    typer.echo()


@app.command()
def compile(
    path: Path = typer.Argument(Path("."), help="Project directory to compile."),
    out: Path = typer.Option(
        Path("manifest.json"),
        "--out",
        help="Write the manifest JSON to this path (default: manifest.json).",
    ),
    check: bool = typer.Option(
        False, "--check", help="Validate only; don't write the manifest (CI gate)."
    ),
) -> None:
    """Compile a project to a validated manifest (and generate loopy.events).

    Writes the manifest to `manifest.json` by default — the same path `loopy run` reads by
    default — so the common case is just `loopy compile .`. Pass `--check` to validate without
    writing (a pure CI gate), or `--out` to write elsewhere.
    """
    from loopy_core.compile.pipeline import compile_project
    from loopy_core.events.codegen import write_events

    result = compile_project(path)
    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)

    if result.project is not None:
        _print_workflows(result.project)

    if not result.diagnostics.has_errors() and result.project is not None:
        write_events(result.project.registry, path)
        if not check:
            _write_manifest(result.project, out)

    raise typer.Exit(code=result.diagnostics.exit_code())


def _write_manifest(project, out: Path) -> None:  # noqa: ANN001 - compile.model.Project
    """Serialize a compiled project to `out` as the manifest JSON (with version + timestamp)."""
    import datetime

    from loopy_core import __version__
    from loopy_core.compile.manifest import to_manifest

    manifest = to_manifest(project)
    manifest["compiled_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    manifest["loopy_version"] = __version__
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


# Source globs that affect a compiled manifest — used for the staleness check below. Deploy-time
# wiring (`--bus`, `--host`, etc.) is consumed at `run`, not compiled into the manifest.
_MANIFEST_SOURCE_DIRS = ("workflows", "skills", "sensors")


def _manifest_is_stale(manifest_path: Path, project_dir: Path) -> bool:
    """True if any compiled source under `project_dir` is newer than the manifest on disk."""
    cutoff = manifest_path.stat().st_mtime
    sources = [project_dir / "registry.yml"]
    for sub in _MANIFEST_SOURCE_DIRS:
        sources.extend((project_dir / sub).rglob("*"))
    return any(p.is_file() and p.stat().st_mtime > cutoff for p in sources)


def _compile_or_exit(project_dir: Path):  # noqa: ANN201 - compile.pipeline.CompileResult
    """Compile `project_dir`, printing diagnostics; exit non-zero if it doesn't compile clean."""
    from loopy_core.compile.pipeline import compile_project

    result = compile_project(project_dir)
    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)
    if result.diagnostics.has_errors() or result.project is None:
        typer.echo(
            typer.style(f"  ✗  compile failed for {project_dir}", fg=typer.colors.RED), err=True
        )
        raise typer.Exit(code=1)
    return result


def _resolve_manifest(target: Path, root: Path) -> tuple[Path, Path]:
    """Resolve a `run`/`trigger` target to a manifest path, compiling from source when apt.

    The manifest stays the artifact `run`/`trigger` consume — but you shouldn't have to compile
    by hand first in the dev loop (dbt doesn't make you `dbt compile` before `dbt run`). So:

      • a **directory** target is a project — always (re)compiled into `<dir>/manifest.json`, and
        that directory becomes the effective `--root`;
      • a **manifest file** with project source alongside `--root` is recompiled only when it's
        missing or stale (some source file is newer);
      • a **manifest file** with no source next to it (the deploy unit — just `manifest.json`)
        is loaded verbatim, never recompiled.

    Returns `(manifest_path, effective_root)`, both absolute.
    """
    from loopy_core.events.codegen import write_events

    target = Path(target)
    root = Path(root)

    if target.is_dir():
        manifest_path = target / "manifest.json"
        result = _compile_or_exit(target)
        write_events(result.project.registry, target)
        _write_manifest(result.project, manifest_path)
        typer.echo(f"loopy: compiled {target} → {manifest_path.name}")
        return manifest_path.resolve(), target.resolve()  # a project dir is its own root

    # A manifest file path. Recompile from --root only when source is actually present there;
    # otherwise this is a prebuilt artifact (the deploy case) and must be loaded as-is.
    if (root / "registry.yml").is_file():
        missing = not target.exists()
        if missing or _manifest_is_stale(target, root):
            result = _compile_or_exit(root)
            write_events(result.project.registry, root)
            _write_manifest(result.project, target)
            verb = "compiled" if missing else "recompiled (source changed)"
            typer.echo(f"loopy: {verb} {root} → {target}")
    elif not target.exists():
        typer.echo(
            f"error: {target} not found, and no project (registry.yml) at {root} to compile "
            f"from. Pass a project directory, or run from the project root.",
            err=True,
        )
        raise typer.Exit(code=1)

    return target.resolve(), root.resolve()


@app.command()
def doctor(
    path: Path = typer.Argument(Path("."), help="Project directory to check."),
) -> None:
    """Preflight a project for its first real run.

    `loopy compile` proves the manifest is *valid*; `doctor` proves it's *runnable* — it catches
    the scaffold defaults that pass a naive presence check but still fail a real run: a
    placeholder API key, the unpushable starter repo, and missing git auth. When a GitHub App is
    configured it also makes the live check — minting a token to confirm the App is installed and
    that the repos in registry.yml are in its selected repositories — so an uninstalled or
    wrong-repos App is caught here rather than at the first `run`/`trigger`.
    """
    from loopy_core.compile.pipeline import compile_project

    # A project that doesn't compile can't be reasoned about — send the user to fix that first.
    result = compile_project(path)
    if result.diagnostics.has_errors() or result.project is None:
        for diagnostic in result.diagnostics.items:
            typer.echo(diagnostic.render(), err=True)
        typer.echo(
            typer.style("  ✗  fix compile errors first (loopy compile .)", fg=typer.colors.RED),
            err=True,
        )
        raise typer.Exit(code=1)

    findings = _diagnose_runnability(Path(path), result.project)

    typer.echo()
    if not findings:
        typer.echo(
            typer.style("  ✓  ready to run", fg=typer.colors.GREEN, bold=True)
            + typer.style(
                "   — nothing blocking a first run",
                fg=typer.colors.BRIGHT_BLACK,
            )
        )
        typer.echo()
        return

    has_errors = _render_findings(findings)
    typer.echo()
    # Warnings alone don't fail the run, so they don't fail doctor; errors do.
    raise typer.Exit(code=1 if has_errors else 0)


def _diagnose_runnability(root: Path, project):  # noqa: ANN001 - compile.model.Project
    """Run the `doctor` preflight against an already-compiled project.

    Shared by `loopy doctor` and the `loopy init` wizard so both report the exact same gaps.
    Reads the sandbox env_files off disk and merges the control-plane env (real env wins over
    `loopy.env`), then defers to the pure `diagnose`. When a GitHub App is configured, it also
    makes the one live call `diagnose` can't — confirming the repos in registry.yml are in the
    App's selected repositories — so an installed-but-wrong-repos App is caught here, not at run.
    """
    from loopy_cli.doctor import check_repo_access, diagnose
    from loopy_runtime.scm.github_app import AppCredentials, GitHubAppError
    from loopy_runtime.secrets import _parse_dotenv, load_control_plane_env

    def read_env(rel: str) -> dict[str, str] | None:
        env_path = root / rel
        return _parse_dotenv(env_path.read_text()) if env_path.is_file() else None

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)  # real/platform env wins over loopy.env

    findings = diagnose(project.registry, read_env=read_env, control_plane_env=merged)

    # With an App configured, verify its installation actually reaches the declared repos —
    # the live gap `diagnose` is blind to. Skipped with no App (diagnose already warns about
    # missing git auth); a key that won't load is left to the run path's loud error.
    if merged.get("GITHUB_APP_ID"):
        try:
            creds = AppCredentials.from_env(merged, root=root)
        except GitHubAppError:
            creds = None
        if creds is not None:
            findings.extend(check_repo_access(project.registry, creds))

    # GitHub webhook wiring — a project can pass everything above and still never hear a
    # `Github.*` event because nothing was registered on GitHub's side. Self-gating: only
    # projects with /hooks/github sensors get findings, and only an App-configured one is
    # checked live.
    findings.extend(registration_findings(project, Path(root), control_env=merged))

    return findings


def _warn_placeholder_env(root: Path, manifest: Path) -> None:
    """Emit non-blocking warnings for env_file values that look like unfilled placeholders.

    The safety net for the user who skips `loopy doctor`: `run` still checks key *presence* via
    `runtime.preflight()`, but a value like `sk-ant-...` or an inline-commented secret passes
    that and only breaks once an agent runs. Warn-only by design — the heuristic can false-
    positive on a real secret, so it must never block a run. Any load hiccup is swallowed: the
    real run path re-loads the manifest right after and reports load errors properly.
    """
    from loopy_cli.doctor import placeholder_warnings
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.secrets import _parse_dotenv

    try:
        m = load_manifest(manifest)
    except Exception:  # noqa: BLE001 - the main path re-loads and reports any load error
        return

    env_files: list[str] = []
    for sandbox in m.registry.sandboxes.values():
        for rel in sandbox.env_file:
            if rel not in env_files:
                env_files.append(rel)

    def read_env(rel: str) -> dict[str, str] | None:
        path = root / rel
        return _parse_dotenv(path.read_text()) if path.is_file() else None

    for finding in placeholder_warnings(read_env=read_env, env_files=env_files):
        typer.echo(typer.style(f"warning: {finding.message}", fg=typer.colors.YELLOW), err=True)
        if finding.hint:
            typer.echo(
                "         " + typer.style(f"→ {finding.hint}", fg=typer.colors.BRIGHT_BLACK),
                err=True,
            )


def _render_findings(findings) -> bool:
    """Print doctor findings (error/warn) with their hints; return True if any are errors."""
    for finding in findings:
        if finding.level == "error":
            mark, color = "✗", typer.colors.RED
        else:
            mark, color = "!", typer.colors.YELLOW
        typer.echo("  " + typer.style(f"{mark}  {finding.message}", fg=color))
        if finding.hint:
            typer.echo("      " + typer.style(f"→ {finding.hint}", fg=typer.colors.BRIGHT_BLACK))
    return any(f.level == "error" for f in findings)


def _make_token_provider(root: Path, *, enabled: bool, announce: bool):
    """Mint scoped GitHub App tokens for sandboxes when an App is configured.

    Reads the control-plane env (`loopy.env`, merged under the process env) for the App
    credentials `loopy auth github` lands there; the App private key stays at the
    control-plane and only the minted, repo-scoped token crosses into a sandbox. Returns a
    `GitHubAppTokenProvider`, or `None` when no App is configured (unchanged offline
    behavior). `enabled=False` (the `--no-tokens` opt-out) skips minting entirely.
    """
    if not enabled:
        return None
    from loopy_runtime.scm.github_app import AppCredentials, GitHubAppError
    from loopy_runtime.scm.token_provider import GitHubAppTokenProvider
    from loopy_runtime.secrets import load_control_plane_env

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)  # real/platform env wins over loopy.env
    if not merged.get("GITHUB_APP_ID"):
        return None  # no App configured at all → no token injection (offline-friendly)
    try:
        creds = AppCredentials.from_env(merged, root=root)
    except GitHubAppError as exc:
        # The App *is* configured (GITHUB_APP_ID is set) but its key won't load. Fail loudly
        # with the real reason rather than silently skipping injection — the latter resurfaces
        # downstream as a misleading "no GitHub auth is configured" at preflight, sending the
        # user to re-run `loopy auth github` when the actual problem is the key itself.
        raise RuntimeError(
            f"a GitHub App is configured (GITHUB_APP_ID is set) but its private key could not "
            f"be loaded: {exc}. Re-run `loopy auth github`, or set GITHUB_APP_PRIVATE_KEY "
            f"(or GITHUB_APP_PRIVATE_KEY_FILE) in the environment."
        ) from exc
    if announce:
        typer.echo("github app: minting scoped tokens for sandboxes (key stays control-plane)")
    return GitHubAppTokenProvider(creds)


def build_runtime(
    manifest,
    *,
    root: Path,
    bus,
    state=None,
    tokens=None,
):
    """Construct the InMemoryRuntime with its standard dependency wiring.

    The single runtime-construction site shared by `run` and `trigger`, so the serve and
    one-shot paths can't drift in how the harness, sandboxes, secrets, bus, durable state,
    and SCM token provider are wired (that drift is exactly what let `trigger` ship without
    token injection). `state` is passed through when given (so a networked bus can share the
    runtime's StateStore); omitted otherwise so the runtime uses its own default.

    Sandbox selection is a project property, not a launch flag: the `RoutingSandboxProvider`
    dispatches each step to the backend named by its sandbox's `provider:` in registry.yml.
    Likewise the cascade spend cap is read from the manifest's `registry.limits.cascade_spend`
    — so both are enforced identically however the engine is started.
    """
    from loopy_runtime.harness.router import HarnessRouter
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import RoutingSandboxProvider
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, EnvFileSecretsResolver

    limits = manifest.registry.limits
    cascade_budget_usd = (
        limits.cascade_spend.get("usd") if limits and limits.cascade_spend else None
    )
    extra = {"state": state} if state is not None else {}
    return InMemoryRuntime(
        manifest,
        harness=HarnessRouter(manifest.registry.agents, manifest.registry.events),
        sandboxes=RoutingSandboxProvider(),
        secrets=EnvFileSecretsResolver(root),
        bus=bus,
        tokens=tokens,
        github_auth_hint=str(root / CONTROL_PLANE_ENV_FILE),
        cascade_budget_usd=cascade_budget_usd,
        **extra,
    )


def _run_record(run_id, runtime, outputs) -> dict:
    """A JSON-serializable record of a finished `trigger` run: the step order, emitted events,
    each completed step's output fields, and any recorded run failures. Pure (no I/O) so the
    CLI's two render paths (human + `--json`) share one shape and it's unit-testable."""
    return {
        "run": run_id,
        "steps": list(runtime.execution_log),
        "emitted": list(runtime.emitted_log),
        "outputs": {name: dict(out.fields) for name, out in outputs.items()},
        "failed": [{"run_id": s.run_id, "error": s.error} for s in runtime.failed_runs],
    }


def _source_root() -> Path | None:
    """The loopy source checkout (the dir holding pyproject.toml), or None.

    Container mode builds the engine image from source, so it needs the build context. Walks up
    from this package; returns None for a wheel install with no source tree alongside it.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _run_in_docker(
    *, root: Path, manifest: Path, port: int | None, detach: bool, build: bool
) -> None:
    """Bring up the single-node stack (engine + redis) via the bundled compose file.

    The Docker plumbing is an implementation detail: everything compose needs is derived from
    the same flags `loopy run` already takes (`--root`, the manifest, `--port`) plus the
    project's `loopy.env` (Daytona creds), and passed through the child's environment — there
    is no user-authored compose file or `.env`. Sandbox selection rides the manifest
    (each sandbox's `provider:`), not the launch command.

    Docker is the only requirement — there is no source-checkout requirement. The engine image
    is tagged by loopy version and built on first use (then reused; `--build` forces a rebuild):
    from a local source tree when one is present, otherwise from the pinned PyPI release via the
    shipped `Dockerfile.pypi`, which needs no build context.
    """
    import shutil
    import subprocess

    from loopy_core import __version__
    from loopy_runtime.secrets import load_control_plane_env

    if shutil.which("docker") is None:
        typer.echo(
            "error: Docker is required for `loopy run` (the container stack). Install Docker, or "
            "use `loopy run --in-process` for the no-Docker dev/testing server.",
            err=True,
        )
        raise typer.Exit(code=1)

    deploy = Path(__file__).resolve().parent / "deploy"
    compose = deploy / "docker-compose.yml"

    # Where to build the engine image from. A source checkout builds the local tree (fast,
    # offline, picks up edits); a pip install with no source builds the pinned PyPI release via
    # Dockerfile.pypi (context is just the deploy dir — no source tree needed).
    source = _source_root()
    if source is not None:
        build_context = source
        dockerfile_rel = os.path.relpath(deploy / "Dockerfile", source)
    else:
        build_context = deploy
        dockerfile_rel = "Dockerfile.pypi"

    root_abs = root.resolve()
    # The container mounts only --root at /project, so the manifest is referenced relative to it.
    # A relative manifest is interpreted against --root (the natural "run from the project" case).
    manifest_abs = manifest if manifest.is_absolute() else (root / manifest)
    manifest_rel = os.path.relpath(manifest_abs.resolve(), root_abs)
    if manifest_rel.startswith(".."):
        typer.echo(
            f"error: manifest {manifest} is outside the project root {root_abs}; "
            "in container mode the manifest must live under --root (it's mounted at /project).",
            err=True,
        )
        raise typer.Exit(code=1)

    # Inherit the parent env, then layer the project's loopy.env (Daytona creds) under it
    # (non-override: a value set in the real environment wins), then the compose substitutions.
    env = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        env.setdefault(key, value)
    env.update(
        {
            "LOOPY_ENGINE_IMAGE": f"loopy-engine:{__version__}",
            "LOOPY_VERSION": __version__,
            "LOOPY_BUILD_CONTEXT": str(build_context),
            "LOOPY_DOCKERFILE": Path(dockerfile_rel).as_posix(),
            "LOOPY_PROJECT": str(root_abs),
            "LOOPY_MANIFEST": Path(manifest_rel).as_posix(),
            "LOOPY_PORT": str(port or 8000),
        }
    )
    cmd = ["docker", "compose", "-f", str(compose), "up"]
    if build:
        cmd.append("--build")
    if detach:
        cmd.append("--detach")
    via = "local source" if source is not None else f"PyPI (loopy-computer=={__version__})"
    typer.echo(
        f"loopy: bringing up engine + redis via docker (image loopy-engine:{__version__}, "
        f"built from {via}; project {root_abs}, port {env['LOOPY_PORT']})"
    )
    raise typer.Exit(code=subprocess.call(cmd, env=env))


@app.command()
def run(
    manifest: Path = typer.Argument(
        Path("manifest.json"),
        help="A manifest.json, or a project directory to compile (default: manifest.json).",
    ),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file + sensors)."),
    host: str | None = typer.Option(None, "--host", help="Sensor webhook bind host."),
    port: int | None = typer.Option(None, "--port", help="Sensor webhook bind port."),
    bus: str | None = typer.Option(
        None,
        "--bus",
        help="EventBus: inproc | redis. Overrides the REDIS_URL auto-detection "
        "(default: redis when REDIS_URL is set, else inproc).",
    ),
    redis_url: str | None = typer.Option(
        None,
        "--redis-url",
        help="Redis URL (or REDIS_URL env var). Selects the redis bus on its own unless --bus says "
        "otherwise.",
    ),
    state: str | None = typer.Option(
        None, "--state", help="StateStore: sqlite | inproc (default sqlite)."
    ),
    state_path: str | None = typer.Option(
        None, "--state-path", help="SQLite state DB path (default .loopy/state.db, under --root)."
    ),
    in_process: bool = typer.Option(
        False,
        "--in-process",
        help="Run the engine in this process — a dev/testing convenience (no Docker).",
    ),
    detach: bool = typer.Option(
        False, "--detach", "-d", help="Run the container stack in the background."
    ),
    build: bool = typer.Option(
        False,
        "--build/--no-build",
        help="Force a rebuild of the engine image (it's built once per version, then reused).",
    ),
) -> None:
    """Start Loopy: bring up the engine + redis container stack (redis bus, sqlite state) hosting
    the sensor webhooks. Requires Docker; the engine image is built once per version and reused.
    `--in-process` runs the engine in this process instead — a dev/testing convenience (no Docker),
    not a fallback. Either way, agents run in the sandboxes each `provider:` names in registry.yml
    (so a `daytona` sandbox still needs DAYTONA_API_KEY)."""
    import asyncio

    _enable_progress_logging()  # surface sandbox build/boot breadcrumbs (otherwise swallowed)

    # Compile-on-demand: accept a project dir (or a stale/missing manifest next to source) and
    # produce a fresh manifest before either path runs — so the dev loop is one command, while a
    # prebuilt manifest with no source (the deploy unit) is still loaded verbatim. A project-dir
    # target also becomes the effective --root (env_file + sensors live there).
    manifest, root = _resolve_manifest(manifest, root)

    # Non-blocking heads-up on env_file values that look like unfilled placeholders — the same
    # thing `loopy doctor` reports, surfaced here for the user who skips it. Warn-only: presence
    # checks still run in preflight; this never blocks a run (the heuristic can false-positive).
    _warn_placeholder_env(root, manifest)

    # The default is the containerized single-node stack. `--in-process` opts into running the
    # engine in *this* process (and is also what the container itself runs internally, so the
    # stack doesn't recurse into launching Docker again). Short-circuit to docker before the
    # in-process wiring below.
    if not in_process:
        _run_in_docker(root=root, manifest=manifest, port=port, detach=detach, build=build)
        return

    from loopy_runtime.bus.factory import make_event_bus
    from loopy_runtime.config import ConfigError, resolve, resolve_redis_url
    from loopy_runtime.manifest_model import ManifestSchemaError, load_manifest
    from loopy_runtime.receiver import LocalEventReceiver
    from loopy_runtime.secrets import load_control_plane_env, load_sensor_env
    from loopy_runtime.sensors.loader import (
        builtin_webhook_sensor,
        load_poll_sensor,
        load_webhook_sensor,
    )
    from loopy_runtime.sensors.runner import FastAPISensorRunner, synthesizing_publisher
    from loopy_runtime.sensors.scheduler import PollScheduler, parse_interval
    from loopy_runtime.state.factory import make_state_store

    # Control-plane infra creds (REDIS_URL, DAYTONA_API_KEY/URL): a local-dev convenience read
    # from `loopy.env`, merged with setdefault (real/platform env always wins). Must land before
    # resolve_redis_url() and any Daytona client creation, both of which read os.environ.
    control_env = load_control_plane_env(root)
    for key, value in control_env.items():
        os.environ.setdefault(key, value)
    if control_env:
        typer.echo(f"loaded {len(control_env)} control-plane var(s) from {root}/loopy.env")

    try:
        cfg = resolve(
            host=host,
            port=port,
            bus=bus,
            state_backend=state,
            state_path=state_path,
            redis_url=redis_url,
        )
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    resolved_redis_url = resolve_redis_url(redis_url)

    try:
        m = load_manifest(manifest)
    except FileNotFoundError as exc:
        typer.echo(
            f"error: manifest {manifest} not found. Run `loopy compile <project> "
            f"--out {manifest}` first, or pass the manifest path.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except ManifestSchemaError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    # Sensor-layer secrets: a single runner-wide `sensors/.env`, merged into the process env so
    # in-process @sensor functions read them via os.environ. Non-override (setdefault) so a value
    # explicitly set in the real environment wins, matching dotenv convention.
    sensor_env = load_sensor_env(root)
    for key, value in sensor_env.items():
        os.environ.setdefault(key, value)
    if sensor_env:
        typer.echo(f"loaded {len(sensor_env)} sensor secret(s) from {root}/sensors/.env")
    # One StateStore shared by the runtime (run history/watermarks) and the bus (at-least-once
    # dedupe by Event.id) — a networked bus consumes off the broker, so it needs its own dedupe.
    # Defaults to a durable SQLite file so history survives restarts and `loopy admin` can read it.
    state = make_state_store(cfg.state_backend, cfg.state_path, root=root)
    event_bus = make_event_bus(cfg.bus, redis_url=resolved_redis_url, state=state)
    try:
        # SCM token injection: if a GitHub App is configured (via `loopy auth github`), mint a
        # scoped installation token per step and inject it into the sandbox. The App private key
        # stays here at the control-plane; only the ephemeral token crosses into the sandbox.
        tokens = _make_token_provider(root, enabled=True, announce=True)
        runtime = build_runtime(
            m,
            root=root,
            bus=event_bus,
            state=state,
            tokens=tokens,
        )
        runtime.preflight()  # fail fast at startup if any sandbox can't supply its harness keys
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    receiver = LocalEventReceiver(event_bus, m.registry.events)  # shared gate for webhooks + polls
    sensor_runner = FastAPISensorRunner(receiver)
    # Each built-in provider posts every event type to one URL and signs the raw body; verify
    # at the edge when that provider's secret is configured. Keyed by path prefix (not sensor
    # source) so hand-written sensors sharing a built-in path — e.g. examples/github — stay
    # verified too. Absent a secret we run unverified (dev only) and say so loudly, once per
    # provider.
    from loopy_runtime.scm import github_webhook, sentry_webhook

    edge_verifiers = {
        "/hooks/github": ("GITHUB_WEBHOOK_SECRET", github_webhook.signature_verifier),
        "/hooks/sentry": ("SENTRY_WEBHOOK_SECRET", sentry_webhook.signature_verifier),
    }
    warned_unverified: set[str] = set()
    for sensor in m.sensors:
        if sensor.trigger.kind != "webhook" or not sensor.trigger.path:
            continue
        try:
            if sensor.source == "builtin":
                fn = builtin_webhook_sensor(sensor)  # platform-shipped mapper, no user code
            else:
                fn = load_webhook_sensor(sensor, root)  # run the real @sensor function
        except Exception as exc:  # noqa: BLE001 - any load failure degrades gracefully
            typer.echo(
                f"warning: sensor '{sensor.name}' not loadable ({exc}); synthesizing events",
                err=True,
            )
            fn = synthesizing_publisher(m, sensor)
        verify = None
        for path_prefix, (secret_env, make_verifier) in edge_verifiers.items():
            if not sensor.trigger.path.startswith(path_prefix):
                continue
            secret = os.environ.get(secret_env)
            if secret:
                verify = make_verifier(secret)
            elif path_prefix not in warned_unverified:
                typer.echo(
                    f"warning: {secret_env} not set; {path_prefix} signatures are "
                    "unverified (dev only — set it before exposing this endpoint)",
                    err=True,
                )
                warned_unverified.add(path_prefix)
            break
        sensor_runner.register_webhook(sensor.trigger.path, fn, verify=verify)

    # Poll sensors run on the in-process scheduler, sharing the runtime's StateStore for
    # watermarks and the same receiver -> bus -> serve() delivery path as webhooks.
    scheduler = PollScheduler(receiver=receiver, state=runtime.state)
    for sensor in m.sensors:
        if sensor.trigger.kind != "poll" or not sensor.trigger.interval:
            continue
        try:
            poll_fn = load_poll_sensor(sensor, root)
            interval = parse_interval(sensor.trigger.interval)
        except Exception as exc:  # noqa: BLE001 - a bad poll sensor is skipped, not fatal
            typer.echo(
                f"warning: poll sensor '{sensor.name}' not loadable ({exc}); skipping", err=True
            )
            continue
        scheduler.register(sensor.name, interval, poll_fn)

    # Cron entry steps (`on: cron(...)`) ride the same scheduler: each fires `runtime.tick`,
    # which instantiates a run rooted at that entry (no event/bus — the tick *is* the trigger).
    for _wf_name, entry in m.cron_entries():
        if not entry.trigger or not entry.trigger.expr:
            continue

        def _fire(scheduled_at, _id=entry.id):  # bind entry.id per-iteration
            return runtime.tick(_id, scheduled_at)

        scheduler.register_cron(entry.id, entry.trigger.expr, entry.trigger.tz, _fire)

    # The admin dashboard rides the same server as the webhooks, path-routed: deliveries
    # at /hooks/*, dashboard at /admin — so one public URL covers both and the admin
    # endpoint is deterministic ($LOOPY_PUBLIC_URL/admin) on every provider. Open on a
    # loopback bind; behind LOOPY_ADMIN_TOKEN otherwise; not mounted at all on a
    # non-loopback bind without a token (fail-closed by absence).
    from loopy_runtime.dashboard.app import mount_admin
    from loopy_runtime.dashboard.auth import is_loopback_host

    admin_mounted = mount_admin(sensor_runner.app, state, m, host=cfg.host, env=os.environ)

    # The HTTP server carries webhooks, the /admin dashboard, and the root /healthz. Serve it
    # whenever any of those must be reachable: a webhook sensor, a mounted dashboard, or simply
    # a hosted (non-loopback) bind — where a platform / CloudFront health check probes /healthz
    # even for a poll/cron-only project. Only a purely local poll/cron project skips the port.
    hosted = not is_loopback_host(cfg.host)
    serve_http = bool(sensor_runner.webhook_paths) or bool(admin_mounted) or hosted

    if sensor_runner.webhook_paths:
        typer.echo(
            f"serving {len(sensor_runner.webhook_paths)} webhook(s) on {cfg.host}:{cfg.port}: "
            f"{', '.join(sensor_runner.webhook_paths)}"
        )
    elif serve_http:
        extra = " + /admin" if admin_mounted else ""
        typer.echo(f"no webhook sensors; serving /healthz{extra} on {cfg.host}:{cfg.port}")
    else:
        typer.echo("no webhook sensors; web server not started (poll/cron-only)")

    if serve_http:
        if admin_mounted:
            typer.echo(f"admin dashboard at /admin ({admin_mounted})")
        elif hosted:
            typer.echo(
                "note: LOOPY_ADMIN_TOKEN not set — /admin dashboard not mounted on this "
                "non-loopback bind (set it to watch runs remotely)"
            )
        # With LOOPY_PUBLIC_URL set (loopy.env, prompted at `loopy init`), print the full
        # delivery URL per sensor — the exact strings to paste into each source's webhook
        # settings. Without it, point at the setting instead of leaving the user to guess
        # how the local bind maps to a public endpoint.
        public_base = os.environ.get("LOOPY_PUBLIC_URL", "").strip().rstrip("/")
        if sensor_runner.webhook_paths and public_base:
            typer.echo(
                "public delivery URLs: "
                + ", ".join(f"{public_base}{p}" for p in sensor_runner.webhook_paths)
            )
            if admin_mounted:
                typer.echo(f"public admin URL: {public_base}/admin")
        elif sensor_runner.webhook_paths:
            typer.echo(
                "note: LOOPY_PUBLIC_URL not set — set it in loopy.env to print each "
                "sensor's public delivery URL (base + path)"
            )
    typer.echo(
        f"polling {len(scheduler.poll_names)} sensor(s): "
        f"{', '.join(scheduler.poll_names) or '(none)'}"
    )
    typer.echo(
        f"cron triggers ({len(scheduler.cron_names)}): "
        f"{', '.join(scheduler.cron_names) or '(none)'}"
    )
    typer.echo(f"event bus: {cfg.bus}" + (f" ({resolved_redis_url})" if cfg.bus == "redis" else ""))
    typer.echo(
        f"state store: {cfg.state_backend}"
        + (f" ({cfg.state_path})" if cfg.state_backend == "sqlite" else "")
    )

    async def _serve() -> None:  # pragma: no cover - exercised by running the server
        consumer = asyncio.create_task(runtime.serve())  # drain runs in the background
        broker = asyncio.create_task(event_bus.run())  # networked bus consume loop (no-op inproc)
        poller = asyncio.create_task(scheduler.start())  # poll + cron triggers fire on their tasks
        background = [consumer, broker, poller]
        try:
            if serve_http:
                await sensor_runner.start(cfg.host, cfg.port)  # uvicorn owns the foreground
            else:
                # Purely local poll/cron: no inbound HTTP, so don't spin up uvicorn —
                # stay alive on the background tasks (the scheduler/consumer) until cancelled.
                await asyncio.gather(*background)
        finally:
            for task in background:
                task.cancel()
            await runtime.sandboxes.aclose()  # release provider HTTP clients (e.g. Daytona)

    asyncio.run(_serve())  # pragma: no cover


@app.command()
def trigger(
    manifest: Path = typer.Argument(
        ..., help="A manifest.json, or a project directory to compile."
    ),
    event: str = typer.Option(..., "--event", help="Triggering event name."),
    fields: str | None = typer.Option(None, "--fields", help="Event fields as a JSON object."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
    no_tokens: bool = typer.Option(
        False, "--no-tokens", help="Skip GitHub App token injection (for fully offline tests)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the full run record (steps, outputs, emits, failures) as JSON."
    ),
) -> None:
    """Fire one event at the manifest and run the cascade to completion (for testing)."""
    import asyncio
    from datetime import UTC, datetime

    # A one-shot probe against a remote sandbox is otherwise silent through the multi-minute
    # build+boot; surface the runtime's lifecycle breadcrumbs so it doesn't look hung.
    _enable_progress_logging()

    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.contract import Event
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.secrets import load_control_plane_env

    # Compile-on-demand, same as `run`: a project-dir target (or stale manifest beside source)
    # is compiled fresh; a prebuilt manifest is loaded as-is. A dir target also sets --root.
    manifest, root = _resolve_manifest(manifest, root)

    # Same non-blocking placeholder heads-up as `run` (see `_warn_placeholder_env`).
    _warn_placeholder_env(root, manifest)

    # Control-plane infra creds (DAYTONA_API_KEY/URL, REDIS_URL) from `loopy.env`, merged with
    # setdefault (real/platform env always wins). `run` does this too; `trigger` must as well or
    # a `provider: daytona` sandbox dies with "DAYTONA_API_KEY is not set" even though it's in
    # loopy.env — the Daytona client reads os.environ, which _make_token_provider (App creds only)
    # never touches. Must land before build_runtime, which acquires the sandbox on trigger.
    control_env = load_control_plane_env(root)
    for key, value in control_env.items():
        os.environ.setdefault(key, value)
    if control_env:
        typer.echo(f"loaded {len(control_env)} control-plane var(s) from {root}/loopy.env")

    m = load_manifest(manifest)
    triggering = Event(
        name=event,
        fields=json.loads(fields) if fields else {},
        id="trigger",
        emitted_at=datetime.now(UTC),
    )

    async def _execute(runtime):
        """Fire the event, collect outputs, then tear the provider down — all in one event
        loop so a provider's HTTP client (e.g. Daytona's) is closed in the loop that created
        it, avoiding `Unclosed client session` warnings after a green run."""
        try:
            run_id = await runtime.trigger(triggering)
            # The per-step outputs are the point of a test run (e.g. a created PR URL), so
            # surface them — the engine records each completed step's output in the StateStore.
            outputs = await runtime.state.outputs(run_id) if run_id is not None else {}
            return run_id, outputs
        finally:
            await runtime.sandboxes.aclose()

    try:
        # Like `run`, inject scoped GitHub App tokens when an App is configured (via
        # `loopy auth github`), so the one-shot test path can exercise repo-touching
        # workflows. `--no-tokens` opts out for fully offline tests. Inside the try so a
        # configured-but-broken App surfaces as a clean error, not a traceback.
        tokens = _make_token_provider(root, enabled=not no_tokens, announce=True)
        runtime = build_runtime(
            m,
            root=root,
            bus=InProcessEventBus(),
            tokens=tokens,
        )
        runtime.preflight()  # fail fast before firing the event if any sandbox lacks its keys
        run_id, outputs = asyncio.run(_execute(runtime))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if run_id is None:
        typer.echo(f"no workflow subscribes to event '{event}'", err=True)
        raise typer.Exit(code=1)

    record = _run_record(run_id, runtime, outputs)

    if as_json:
        typer.echo(json.dumps(record, indent=2, default=str))
    else:
        typer.echo(f"run: {record['run']}")
        typer.echo(f"steps: {' -> '.join(record['steps'])}")
        typer.echo(f"emitted: {', '.join(record['emitted']) or '(none)'}")
        for name, fields in record["outputs"].items():
            if fields:
                typer.echo(f"output[{name}]: {json.dumps(fields, default=str)}")
        for status in record["failed"]:  # a recorded run failure → report on stderr
            typer.echo(f"FAILED {status['run_id']}: {status['error']}", err=True)

    if record["failed"]:
        raise typer.Exit(code=1)


def _admin_port(flag: int | None) -> int:
    """Resolve the dashboard port: `--port` flag > platform-injected `$PORT` > 9000.

    Honoring `$PORT` is half of the provider-agnostic serve contract (the other half is the
    env-var token) — Render/Fly/Railway all tell the process where to bind this way."""
    if flag is not None:
        return flag
    raw = os.environ.get("PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            typer.echo(f"warning: ignoring non-numeric $PORT={raw!r}", err=True)
    return 9000


def _admin_proxy_url() -> str:
    """The proxied control-plane URL: the deterministic admin endpoint derived from
    `LOOPY_PUBLIC_URL` — the engine mounts the dashboard at `/admin` on the same server that
    receives webhook deliveries, so one public URL covers both."""
    public = os.environ.get("LOOPY_PUBLIC_URL", "").strip().rstrip("/")
    if not public:
        typer.echo(
            "error: `loopy admin` derives its URL from LOOPY_PUBLIC_URL, which is not set. "
            "Set it in the environment or loopy.env, or pass "
            "`--url https://loopy.example.com/admin`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return f"{public}/admin"


def _admin_tunnel_url(root: Path) -> str:
    """The dashboard URL for a tunneled deploy: the local end of its SSM port-forward tunnel.

    Behind CloudFront `/admin` is blocked (the edge→origin hop is plain HTTP, so the bearer
    token must not travel it); the dashboard is reached over an SSM tunnel to the engine port
    instead. `loopy deploy bootstrap` records the instance id and engine port in loopy.env, so
    the tunnel command printed here is ready to paste; both fall back to placeholders/defaults
    when the deploy predates the recording (or the URL is a CloudFront one from elsewhere).
    """
    from loopy_cli.deploy_target import (
        BOOTSTRAP_ENGINE_PORT_ENV,
        BOOTSTRAP_INSTANCE_ID_ENV,
        resolve_bootstrap_config,
    )

    config = resolve_bootstrap_config(root)
    engine_port = config.get(BOOTSTRAP_ENGINE_PORT_ENV, "8000")
    instance = config.get(BOOTSTRAP_INSTANCE_ID_ENV, "<instance-id>")
    typer.echo("CloudFront deploy: the dashboard rides an SSM tunnel to the engine port.")
    typer.echo("If it isn't up yet, start it in another terminal (needs the Session")
    typer.echo("Manager plugin, a one-time install; see AWS docs):")
    typer.echo(f"  aws ssm start-session --target {instance} \\")
    typer.echo("    --document-name AWS-StartPortForwardingSession \\")
    typer.echo(
        f'    --parameters \'{{"portNumber":["{engine_port}"],'
        f'"localPortNumber":["{engine_port}"]}}\''
    )
    return f"http://localhost:{engine_port}/admin"


@app.command()
def admin(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int | None = typer.Option(
        None, "--port", help="Port to serve the dashboard on (default $PORT, then 9000)."
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Attach to the local runner's state DB, even when LOOPY_PUBLIC_URL is set.",
    ),
    tunnel: bool = typer.Option(
        False,
        "--tunnel",
        help="Reach a CloudFront-fronted deploy over its SSM tunnel (prints the tunnel command).",
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="State DB to read (local dashboard only; default .loopy/state.db).",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        help="Control-plane URL to proxy to (overrides the URL derived from LOOPY_PUBLIC_URL).",
    ),
    manifest: Path = typer.Option(
        Path("manifest.json"),
        "--manifest",
        help="Compiled manifest for the templates/registry/schedules views (default manifest.json; "
        "skipped if absent).",
    ),
) -> None:
    """Serve the read-only control-plane dashboard.

    With no flags, `loopy admin` routes itself off LOOPY_PUBLIC_URL:

    - unset → the local run-state DB `loopy run` writes on this machine (the dev loop: `loopy
      run` in one terminal, `loopy admin` in another). When `manifest.json` is present it also
      powers the workflow-template, registry, and schedule views; the run views work without it.
    - a CloudFront URL (`*.cloudfront.net`, or a recorded bootstrap instance) → an SSM tunnel to
      the engine port. Behind CloudFront `/admin` is blocked (the edge→origin hop is plain HTTP,
      so the bearer token can't ride it); the tunnel command is printed for you.
    - any other URL → a local loopback proxy that serves the UI and forwards /api to
      $LOOPY_PUBLIC_URL/admin with a bearer token from LOOPY_ADMIN_TOKEN, so the browser never
      holds the credential.

    Override the routing with `--local` (force the local DB), `--tunnel` (force the SSM tunnel),
    or `--url` (proxy somewhere explicit). Those three are mutually exclusive.

    Exposed server (`--local --host 0.0.0.0`, e.g. on the control plane itself): requires
    LOOPY_ADMIN_TOKEN in the environment and puts every /api route behind it; refuses to start
    without one, because run/step outputs are not redacted. (`loopy run` also mounts this
    dashboard at /admin on its own webhook server, under the same rules.)
    """
    import uvicorn

    from loopy_cli.deploy_target import (
        BOOTSTRAP_INSTANCE_ID_ENV,
        is_cloudfront_url,
        resolve_bootstrap_config,
    )
    from loopy_runtime.dashboard.auth import AdminAuth, is_loopback_host
    from loopy_runtime.secrets import ADMIN_TOKEN_ENV, load_control_plane_env

    root = Path.cwd()

    # The admin token rides the control-plane env channel: `loopy.env` on a laptop, the
    # platform's process env in production. setdefault — the real environment always wins.
    for key, value in load_control_plane_env(root).items():
        os.environ.setdefault(key, value)
    port = _admin_port(port)

    # --local / --tunnel / --url each pin a different dashboard; more than one is contradictory.
    picked = [
        name
        for name, on in (("--local", local), ("--tunnel", tunnel), ("--url", url is not None))
        if on
    ]
    if len(picked) > 1:
        typer.echo(
            f"error: {', '.join(picked)} select different dashboards — pass at most one.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Resolve the mode. An explicit flag wins; otherwise route off LOOPY_PUBLIC_URL — a
    # CloudFront deploy (where /admin is blocked at the edge) rides the tunnel, any other URL is
    # proxied at /admin, and no URL at all means the local run-state DB.
    public = os.environ.get("LOOPY_PUBLIC_URL", "").strip().rstrip("/")
    tunnel_signal = is_cloudfront_url(public) or bool(
        resolve_bootstrap_config(root).get(BOOTSTRAP_INSTANCE_ID_ENV)
    )
    if local:
        mode = "local"
    elif tunnel:
        mode = "tunnel"
    elif url is not None:
        mode = "proxy"
    elif public and tunnel_signal:
        mode = "tunnel"
    elif public:
        mode = "proxy"
    else:
        mode = "local"

    if mode != "local" and db is not None:
        typer.echo(
            "error: --db reads a local state DB, which a proxied/tunneled dashboard doesn't "
            "use — drop it, or pass `loopy admin --local --db PATH`.",
            err=True,
        )
        raise typer.Exit(code=1)

    if mode in ("proxy", "tunnel"):
        from loopy_runtime.dashboard.proxy import create_proxy_app, validate_remote_url

        if not is_loopback_host(host):
            typer.echo(
                "error: `loopy admin` runs a local proxy that holds the admin token; it binds "
                "loopback only (drop --host, or harden the control plane itself instead).",
                err=True,
            )
            raise typer.Exit(code=1)
        if url is None:
            url = _admin_tunnel_url(root) if mode == "tunnel" else _admin_proxy_url()
        try:
            url = validate_remote_url(url)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        token = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
        if not token:
            typer.echo(
                f"error: `loopy admin` needs {ADMIN_TOKEN_ENV} in the environment or loopy.env "
                "— the same token the control plane was deployed with.",
                err=True,
            )
            raise typer.Exit(code=1)
        port = _resolve_dashboard_port(host, port)
        typer.echo(f"loopy dashboard → http://{host}:{port}  ({mode} → {url})")
        config = uvicorn.Config(
            create_proxy_app(url, token), host=host, port=port, log_level="warning"
        )
        _serve_dashboard(config)  # pragma: no cover - long-lived server
        return

    assert mode == "local"

    from loopy_runtime.dashboard.app import create_app
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.state.sqlite import SqliteStateStore

    # Fail-closed (checked before anything else): run/step outputs are not redacted, so a
    # bind that leaves loopback must carry bearer auth or not start at all.
    auth = None
    if not is_loopback_host(host):
        auth = AdminAuth.from_env(os.environ)
        if auth is None:
            typer.echo(
                f"error: refusing to bind {host} without {ADMIN_TOKEN_ENV}: run and step "
                f"outputs are not redacted. `loopy init` mints a {ADMIN_TOKEN_ENV} into "
                "loopy.env; set that same value in the platform environment. (No project yet? "
                "Run `loopy init`.)",
                err=True,
            )
            raise typer.Exit(code=1)

    db = db if db is not None else Path(".loopy/state.db")
    try:
        store = SqliteStateStore(db, read_only=True)  # raises if the DB doesn't exist yet
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        # A local DB is expected before a local run. If instead this project deploys to a hosted
        # engine, `loopy admin` reaches it off LOOPY_PUBLIC_URL once that's set (a CloudFront
        # deploy rides the tunnel automatically, or force it with `loopy admin --tunnel`) — no
        # deploy target is recorded to detect that here, so the pointer is unconditional.
        if not public:
            typer.echo(
                "       (deploying to a hosted engine? once it's up and LOOPY_PUBLIC_URL is "
                "set, `loopy admin` targets it automatically — a CloudFront deploy rides the "
                "tunnel, or force it with `loopy admin --tunnel`)",
                err=True,
            )
        raise typer.Exit(code=1) from exc

    loaded = None
    if manifest.is_file():
        try:
            loaded = load_manifest(manifest)
        except Exception as exc:  # noqa: BLE001 — a bad manifest shouldn't block the run views
            typer.echo(f"warning: ignoring {manifest} ({exc})", err=True)
    else:
        typer.echo(f"note: no {manifest} — templates/registry/schedules views will be empty")

    port = _resolve_dashboard_port(host, port)
    extra = "" if loaded is None else f"  (manifest {manifest})"
    guard = "" if auth is None else "  (bearer auth on /api)"
    typer.echo(f"loopy dashboard → http://{host}:{port}  (reading {db}){extra}{guard}")
    config = uvicorn.Config(
        create_app(store, loaded, auth=auth), host=host, port=port, log_level="warning"
    )
    _serve_dashboard(config)  # pragma: no cover - long-lived server


@app.command()
def demo(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(
        9001,
        "--port",
        help="Port to serve the dashboard on (default 9001, distinct from `loopy admin`'s 9000 "
        "so the demo can't squat on the real dashboard's port).",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't try to open the dashboard in a browser."
    ),
) -> None:
    """Serve the admin dashboard against in-memory fake data — for developing the dashboard.

    A throwaway convenience: no project, compile, DB, or network. It synthesizes a manifest and
    a seeded run store in process so every view (runs, schedules, workflows, registry) has
    something to show. Safe to delete along with `loopy_runtime/dashboard/demo.py`.
    """
    import asyncio
    import webbrowser

    import uvicorn

    from loopy_runtime.dashboard.app import create_app
    from loopy_runtime.dashboard.demo import build_demo_manifest, seed_demo_store

    store = asyncio.run(seed_demo_store())
    manifest = build_demo_manifest()

    port = _resolve_dashboard_port(host, port)
    url = f"http://{host}:{port}"
    typer.echo(f"loopy demo dashboard → {url}  (fake data, in-memory — not a real deployment)")
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — no browser (e.g. headless) is fine; the URL is printed
            pass
    config = uvicorn.Config(
        create_app(store, manifest, demo=True), host=host, port=port, log_level="warning"
    )
    _serve_dashboard(config)  # pragma: no cover - long-lived server


# ── Deploy artifact generators (`loopy dockerfile`, `loopy env`) ────────────────────────
# The engine deploys from a git push on any platform that reads a root Dockerfile. Rather than
# scaffold these into every project (where they'd drift from the engine version), they're
# generated on demand and pinned to the installed release.

_DOCKERFILE_TEMPLATE = """\
# Generated by `loopy dockerfile` (loopy {version}). Regenerate after upgrading loopy so the
# pinned release below matches your CLI. This builds the engine image a platform (Render,
# Railway, Fly, ...) runs straight from a git push: the project is copied in and compiled at
# build time. Agents do NOT run here — each step runs in the Daytona sandbox its `provider:`
# names in registry.yml.
FROM python:3.12-slim

# Pinned to the loopy release this file was generated from, so the image never drifts from the
# CLI you compile with.
RUN pip install --no-cache-dir "loopy-computer[redis]=={version}"

WORKDIR /project
COPY . /project

# Build gate: a project that doesn't compile fails the image build (and writes manifest.json).
RUN loopy compile .

# One long-lived process: sensor webhooks + scheduler + runtime. No --bus flag — the engine uses
# Redis when REDIS_URL is set (managed Redis in production) and the in-process bus otherwise. Run
# history lives at .loopy/state.db; for durability across redeploys, mount a volume and add
# `--state-path /state/state.db` to the command below.
ENTRYPOINT ["loopy"]
CMD ["run", "manifest.json", "--in-process", "--root", ".", "--host", "0.0.0.0", "--port", "8000"]
"""

_DOCKERIGNORE_TEMPLATE = """\
# Generated by `loopy dockerfile`. Keeps secrets and local cruft out of the Docker build context,
# so a local `docker build` never bakes them into the image. (A platform building from git won't
# see gitignored files, but a local build sees your whole working tree.) No secret file belongs
# in the image: the engine reads secrets from its environment at run time (see `loopy env`), so
# every dotenv file is excluded wherever a sandbox's env_file: points, not just the defaults.
.git
loopy.env
**/*.env
secrets/
.loopy/
manifest.json
__pycache__/
*.pyc
.venv/
"""

# Control-plane keys `loopy env` never emits verbatim: LOOPY_PUBLIC_URL is laptop-side (webhook
# registration + `loopy admin` run there, not on the platform); REDIS_URL gets an editable
# placeholder instead of the local `localhost` value; the rotation slot is transient.
_ENV_DEPLOY_SKIP = frozenset({"LOOPY_PUBLIC_URL", "REDIS_URL", "LOOPY_ADMIN_TOKEN_NEXT"})


@app.command()
def dockerfile(
    path: Path = typer.Argument(Path("."), help="Project directory to generate deploy files in."),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print the Dockerfile to stdout and write nothing."
    ),
) -> None:
    """Generate a version-pinned Dockerfile (+ .dockerignore) for a git-push deploy.

    Writes `Dockerfile` and `.dockerignore` to the project root so a platform (Render, Railway,
    Fly, ...) can build the engine from your repo: the project is copied in and compiled at build
    time, and the pin matches your installed loopy (regenerate after upgrading). These files are
    generated artifacts, so an existing `Dockerfile` / `.dockerignore` is overwritten in place —
    that keeps them current after a loopy upgrade. `--stdout` prints just the Dockerfile — to
    inspect or pipe — and writes nothing.
    """
    from loopy_core import __version__

    content = _DOCKERFILE_TEMPLATE.format(version=__version__)
    if stdout:
        typer.echo(content, nl=False)
        return

    root = Path(path)
    if not root.is_dir():
        typer.echo(f"error: {root} is not a directory.", err=True)
        raise typer.Exit(code=1)
    dockerfile_path = root / "Dockerfile"
    dockerignore_path = root / ".dockerignore"

    dockerfile_path.write_text(content)
    dockerignore_path.write_text(_DOCKERIGNORE_TEMPLATE)
    typer.echo(f"loopy: wrote Dockerfile and .dockerignore (pinned to loopy {__version__})")
    typer.echo(
        "  commit both, then deploy from a git push. Secrets stay out of the image "
        "(see .dockerignore); paste your env with `loopy env`."
    )


@app.command()
def env(
    path: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """Print the production environment block to paste into your platform's env settings.

    Emits, from your local config, the engine's control-plane creds (from loopy.env) and
    REDIS_URL as an editable placeholder. LOOPY_PUBLIC_URL is omitted — it is laptop-side.
    Sandbox secrets are not emitted here: each sandbox reads its own `env_file`, which a deploy
    pushes to the target directly. This prints secrets to stdout on purpose, for a one-shot paste
    into a platform's ".env import" field; it is never logged. Do not commit the output.
    """
    from loopy_runtime.secrets import load_control_plane_env

    # Compile so an uncompilable project errors before we emit anything.
    _compile_or_exit(path)
    root = Path(path)
    control = load_control_plane_env(root)

    lines = ["# --- Control plane (engine) — infra creds; set these on the platform ---"]
    for key in sorted(control):
        if key not in _ENV_DEPLOY_SKIP:
            lines.append(f"{key}={control[key]}")
    lines.append(
        "REDIS_URL=redis://…   # managed Redis connection string; "
        "remove this line to use the in-process bus"
    )
    lines.append(
        "# LOOPY_PUBLIC_URL is laptop-side (webhook registration, loopy admin) — "
        "keep it in your local loopy.env, not here"
    )

    typer.echo("\n".join(lines))


if __name__ == "__main__":  # pragma: no cover
    app()
