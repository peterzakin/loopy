"""Loopy CLI — one binary over both halves.

    loopy compile   produce the manifest (frontend)
    loopy doctor    preflight a project for its first run (placeholders, repo, git auth)
    loopy run       start the server: host sensor webhooks; events drive workflow runs
    loopy trigger   fire one event at the manifest and run it to completion (for testing)
    loopy admin     serve the read-only dashboard over the run-state DB `loopy run` writes

Heavy deps are imported lazily per command so `loopy compile` stays runtime-free.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import typer

from loopy_cli.auth import auth_app

app = typer.Typer(add_completion=False, help="Loopy — compile and run durable agent workflows.")

# `loopy auth ...` — onboarding for external creds (GitHub App manifest flow). The
# sub-app's heavy imports are deferred into its command bodies, so registering it
# here keeps `loopy compile` runtime-free.
app.add_typer(auth_app, name="auth")


@app.callback()
def main() -> None:
    """Author, compile, and run durable agent workflows."""


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
                glyph = ("└─●" if ni == len(gen) - 1 else "├─●")
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
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "-y",
        help="Skip the setup prompts: write the placeholder scaffold verbatim (scripts/CI).",
    ),
) -> None:
    """Scaffold a new Loopy project: registry, a runnable starter workflow, and an env file.

    By default a short wizard offers to close the gaps the scaffold leaves on purpose —
    reusing an `ANTHROPIC_API_KEY` already in your environment, and wiring git auth — then
    reports whatever's still missing (the same checks as `loopy doctor`). `--non-interactive`
    skips the prompts entirely and just writes the placeholder scaffold.
    """
    from loopy_cli.scaffold import InvalidProjectName, scaffold_project, validate_project_name

    # Prompt only when we actually have a human on a terminal; `--non-interactive` forces off.
    interactive = not non_interactive and sys.stdin.isatty()

    if not name:
        if not interactive:
            typer.echo("error: project name is required with --non-interactive", err=True)
            raise typer.Exit(code=1)
        name = typer.prompt("Project name")
    try:
        name = validate_project_name(name)
    except InvalidProjectName as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Ask up front which repo(s) the agent works on so the scaffold lands the real value
    # (a checkout to edit) instead of an unpushable placeholder. Blank is a first-class answer.
    repos = _prompt_for_repos() if interactive else None

    target = (directory / name).resolve()
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

    # Offer to close the gaps the scaffold leaves on purpose before reporting what's left.
    if interactive:
        _offer_ambient_anthropic_key(target)
        _offer_ambient_daytona_creds(target)
        # Git auth only matters if the agent actually clones a repo. With none configured,
        # creating a GitHub App would have nothing to install on — so skip it and say why.
        if repos:
            _offer_github_auth(target)
        else:
            _note_orchestrator_mode()

    # A clean compile is *not* a runnable project: the scaffold ships placeholders on purpose
    # (a fake API key, maybe no git auth). Run the same checks as `loopy doctor` so the user
    # sees the *actual* remaining gaps — anything resolved above is already gone.
    _report_remaining_setup(target, name)


def _prompt_for_repos() -> list[str]:
    """Ask which repo(s) the agent should work on; an empty answer is a first-class choice.

    A repo is what the starter `codefix` workflow clones to edit and open a PR against. But
    loopy is still useful with none — you get a workflow orchestrator that just doesn't touch a
    code repo — so blank means "no repo", not "unfinished", and we never fall back to an
    unpushable placeholder.
    """
    raw = typer.prompt(
        "  Which repo(s) should the agent work on? (owner/repo, comma-separated; "
        "blank = no repo)",
        default="",
        show_default=False,
    )
    return [r.strip() for r in raw.split(",") if r.strip()]


def _note_orchestrator_mode() -> None:
    """Frame a repo-less scaffold as a real, runnable orchestrator — not a half-finished setup."""
    typer.echo(
        "  "
        + typer.style("ⓘ", fg=typer.colors.BLUE)
        + " No repo — scaffolded a workflow orchestrator: a Note → summary + action-items loop."
    )
    typer.echo(
        typer.style(
            "    Try it with `loopy trigger --event Note`. Want code edits instead? Add a repo "
            "to sandboxes.Dev.repos and run `loopy auth github`.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )
    typer.echo()


def _offer_ambient_anthropic_key(target: Path) -> None:
    """If a real `ANTHROPIC_API_KEY` is in the environment, offer to write it into the env_file.

    The scaffold ships a `sk-ant-...` placeholder that compiles green but fails the first run.
    Most people trying loopy already have a key exported, so reuse it on the spot rather than
    making them hand-edit `secrets/dev.env`. Skips silently when no usable key is present.
    """
    from loopy_cli.doctor import PLACEHOLDER_ANTHROPIC_KEY

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key == PLACEHOLDER_ANTHROPIC_KEY:
        return

    env_path = target / "secrets" / "dev.env"
    placeholder_line = f"ANTHROPIC_API_KEY={PLACEHOLDER_ANTHROPIC_KEY}"
    if not env_path.is_file() or placeholder_line not in env_path.read_text():
        return  # nothing to replace (already set, or layout changed) — don't guess

    masked = f"{key[:7]}…{key[-4:]}" if len(key) > 12 else "the value"
    if not typer.confirm(
        f"  Found ANTHROPIC_API_KEY in your environment ({masked}). "
        f"Write it into secrets/dev.env?",
        default=True,
    ):
        return

    text = env_path.read_text().replace(placeholder_line, f"ANTHROPIC_API_KEY={key}")
    env_path.write_text(text)
    typer.echo(
        "  "
        + typer.style("✓", fg=typer.colors.GREEN)
        + " wrote ANTHROPIC_API_KEY to secrets/dev.env"
    )
    typer.echo()


def _offer_ambient_daytona_creds(target: Path) -> None:
    """If `DAYTONA_API_KEY` is in the environment, offer to write it into the control-plane env.

    The default scaffold's sandbox is `provider: daytona`, which needs `DAYTONA_API_KEY` (and
    optionally `DAYTONA_API_URL`) in `loopy.env` to run — the scaffold ships those as commented
    placeholders. Most people trying loopy with Daytona already have the key exported, so reuse
    it on the spot rather than making them hand-edit `loopy.env`. `DAYTONA_API_URL` is only
    carried along when the key is present (a URL alone can't authenticate). Skips silently when
    no key is in the environment, or when `loopy.env` already has one (don't clobber).
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
    typer.echo(
        "  " + typer.style("✓", fg=typer.colors.GREEN) + f" wrote {wrote} to loopy.env"
    )
    typer.echo()


def _offer_github_auth(target: Path) -> None:
    """Offer to wire git auth now by running the `loopy auth github` manifest flow in-process.

    Declining just leaves the step for later — `_report_remaining_setup` will flag it. A failed
    or aborted auth run is caught so it never takes the whole `init` down with it.
    """
    from loopy_runtime.secrets import load_control_plane_env

    # Already configured (e.g. re-init over an existing tree) — nothing to offer.
    if load_control_plane_env(target).get("GITHUB_APP_ID"):
        return

    if not typer.confirm(
        "  Wire git auth now? Runs `loopy auth github` (creates a GitHub App, opens a browser)",
        default=True,
    ):
        return

    from loopy_cli.auth import run_github_auth

    try:
        run_github_auth(root=target)
    except typer.Exit:
        typer.echo("  (skipped — git auth not completed; run `loopy auth github` later)")
    except Exception as exc:  # noqa: BLE001 - never let onboarding crash the scaffold
        typer.echo(f"  (git auth didn't complete: {exc} — run `loopy auth github` later)")


def _report_remaining_setup(target: Path, name: str) -> None:
    """Compile the fresh project and print the gaps still blocking a first run (doctor's checks).

    Replaces the old static checklist: because the wizard may have already set the key or wired
    auth, we report the *actual* remaining findings instead of a fixed list the user has to
    re-verify by hand. Then we point at the run commands.
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

    typer.echo("  Then:")
    typer.echo(typer.style(f"    cd {name}", fg=typer.colors.BRIGHT_WHITE))
    typer.echo(
        typer.style("    loopy doctor", fg=typer.colors.BRIGHT_WHITE)
        + typer.style("   # re-check the above any time", fg=typer.colors.BRIGHT_BLACK)
    )
    typer.echo(
        typer.style("    loopy run", fg=typer.colors.BRIGHT_WHITE)
        + typer.style("   # compiles + starts the engine", fg=typer.colors.BRIGHT_BLACK)
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


# Source globs that affect a compiled manifest — used for the staleness check below. `loopy.yaml`
# is deliberately excluded: it's deploy config consumed at `run`, not compiled into the manifest.
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
    from loopy_cli.doctor import Finding, check_repo_access, diagnose
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
            # Backstop: check_repo_access already degrades known GitHub/network errors to a
            # warn, but a diagnostic command must never surface a raw traceback. Anything it
            # doesn't anticipate becomes the same warn its docstring promises, so a flaky API
            # response can't make a perfectly good scaffold look corrupted.
            try:
                findings.extend(check_repo_access(project.registry, creds))
            except Exception as exc:  # noqa: BLE001 - a preflight must never crash the user
                findings.append(
                    Finding(
                        "warn", f"couldn't verify repo access: {exc}", "run `loopy auth status`"
                    )
                )

    return findings


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
    config: Path = typer.Option(
        Path("loopy.yaml"), "--config", help="Deployment defaults (loopy.yaml); flags override it."
    ),
    host: str | None = typer.Option(None, "--host", help="Override sensor_server.host."),
    port: int | None = typer.Option(None, "--port", help="Override sensor_server.port."),
    bus: str | None = typer.Option(
        None, "--bus", help="EventBus: inproc | redis. Overrides config."
    ),
    redis_url: str | None = typer.Option(
        None, "--redis-url", help="Redis URL (or REDIS_URL env var; used when bus=redis)."
    ),
    state: str | None = typer.Option(
        None, "--state", help="StateStore: sqlite | inproc. Overrides config (default sqlite)."
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

    # The default is the containerized single-node stack. `--in-process` opts into running the
    # engine in *this* process (and is also what the container itself runs internally, so the
    # stack doesn't recurse into launching Docker again). Short-circuit to docker before the
    # in-process wiring below.
    if not in_process:
        _run_in_docker(root=root, manifest=manifest, port=port, detach=detach, build=build)
        return

    from loopy_runtime.bus.factory import make_event_bus
    from loopy_runtime.config import ConfigError, load_config, resolve, resolve_redis_url
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.receiver import LocalEventReceiver
    from loopy_runtime.secrets import load_control_plane_env, load_sensor_env
    from loopy_runtime.sensors.loader import load_poll_sensor, load_webhook_sensor
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
            load_config(config),
            host=host,
            port=port,
            bus=bus,
            state_backend=state,
            state_path=state_path,
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
    # GitHub posts every event type to one URL signed with X-Hub-Signature-256; verify it at the
    # edge when the secret is configured. Paths under /hooks/github opt in; absent a secret we run
    # unverified (dev only) and say so loudly.
    from loopy_runtime.scm.github_webhook import signature_verifier

    github_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    warned_unverified = False
    for sensor in m.sensors:
        if sensor.trigger.kind != "webhook" or not sensor.trigger.path:
            continue
        try:
            fn = load_webhook_sensor(sensor, root)  # run the real @sensor function
        except Exception as exc:  # noqa: BLE001 - any load failure degrades gracefully
            typer.echo(
                f"warning: sensor '{sensor.name}' not loadable ({exc}); synthesizing events",
                err=True,
            )
            fn = synthesizing_publisher(m, sensor)
        verify = None
        if sensor.trigger.path.startswith("/hooks/github"):
            if github_secret:
                verify = signature_verifier(github_secret)
            elif not warned_unverified:
                typer.echo(
                    "warning: GITHUB_WEBHOOK_SECRET not set; /hooks/github signatures are "
                    "unverified (dev only — set it before exposing this endpoint)",
                    err=True,
                )
                warned_unverified = True
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

    if sensor_runner.webhook_paths:
        typer.echo(
            f"serving {len(sensor_runner.webhook_paths)} webhook(s) on {cfg.host}:{cfg.port}: "
            f"{', '.join(sensor_runner.webhook_paths)}"
        )
    else:
        typer.echo("no webhook sensors; web server not started (poll/cron-only)")
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
            if sensor_runner.webhook_paths:
                await sensor_runner.start(cfg.host, cfg.port)  # uvicorn owns the foreground
            else:
                # Poll/cron-only (or no sensors): no inbound HTTP, so don't spin up uvicorn —
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

    # Compile-on-demand, same as `run`: a project-dir target (or stale manifest beside source)
    # is compiled fresh; a prebuilt manifest is loaded as-is. A dir target also sets --root.
    manifest, root = _resolve_manifest(manifest, root)
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


@app.command()
def admin(
    db: Path = typer.Argument(
        Path(".loopy/state.db"), help="State DB written by `loopy run` (default .loopy/state.db)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(9000, "--port", help="Port to serve the dashboard on."),
) -> None:
    """Serve the read-only control-plane dashboard over the run-state DB.

    Pairs with `loopy run` (which defaults to that same DB): no flags needed in the common case —
    `loopy run` in one terminal, `loopy admin` in another.
    """
    import asyncio

    import uvicorn

    from loopy_runtime.dashboard.app import create_app
    from loopy_runtime.state.sqlite import SqliteStateStore

    try:
        store = SqliteStateStore(db, read_only=True)  # raises if the DB doesn't exist yet
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"loopy dashboard → http://{host}:{port}  (reading {db})")
    config = uvicorn.Config(create_app(store), host=host, port=port, log_level="warning")
    asyncio.run(uvicorn.Server(config).serve())  # pragma: no cover - long-lived server


if __name__ == "__main__":  # pragma: no cover
    app()
