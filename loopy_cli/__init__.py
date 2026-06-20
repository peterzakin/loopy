"""Loopy CLI — one binary over both halves.

    loopy compile   produce the manifest (frontend)
    loopy run       start the server: host sensor webhooks; events drive workflow runs
    loopy trigger   fire one event at the manifest and run it to completion (for testing)
    loopy admin     serve the read-only dashboard over the run-state DB `loopy run` writes

Heavy deps are imported lazily per command so `loopy compile` stays runtime-free.
"""

from __future__ import annotations

import json
import os
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


def _print_workflows(project) -> None:  # noqa: ANN001 - loopy_core.compile.model.Project
    """Print every compiled workflow (its trigger and steps) to stdout, with flair."""
    workflows = project.workflows
    count = len(workflows)
    plural = "workflow" if count == 1 else "workflows"
    typer.echo()
    summary = f"  🔁  Loopy compiled {count} {plural}"
    typer.echo(typer.style(summary, fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 40, fg=typer.colors.BRIGHT_BLACK))

    for name, wf in sorted(workflows.items()):
        entry = wf.steps.get(wf.entry) if wf.entry else None
        trigger = entry.trigger if entry else None
        if trigger and trigger.kind == "event":
            icon, trig = "⚡", f"on event {trigger.event}"
        elif trigger and trigger.kind == "cron":
            icon, trig = "⏰", f"on cron({trigger.expr})"
        else:
            icon, trig = "❓", "no trigger"

        typer.echo()
        typer.echo(
            f"  {icon}  "
            + typer.style(name, fg=typer.colors.BRIGHT_WHITE, bold=True)
            + "  "
            + typer.style(trig, fg=typer.colors.YELLOW)
        )

        steps = list(wf.steps.values())
        for i, step in enumerate(steps):
            connector = "└─" if i == len(steps) - 1 else "├─"
            line = "      " + typer.style(connector, fg=typer.colors.BRIGHT_BLACK)
            line += " " + typer.style(step.name, fg=typer.colors.GREEN)
            meta = []
            if step.agent:
                meta.append("🤖 " + typer.style(step.agent, fg=typer.colors.BLUE))
            if step.after:
                meta.append(
                    typer.style("after ", fg=typer.colors.BRIGHT_BLACK)
                    + typer.style(", ".join(step.after), fg=typer.colors.CYAN)
                )
            if step.emits:
                meta.append("📡 " + typer.style(", ".join(step.emits), fg=typer.colors.MAGENTA))
            if meta:
                line += typer.style("  ·  ", fg=typer.colors.BRIGHT_BLACK).join([""] + meta)
            typer.echo(line)
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
    """Scaffold a new Loopy project: registry, a runnable starter workflow, and an env file."""
    from loopy_cli.scaffold import InvalidProjectName, scaffold_project, validate_project_name

    if not name:
        name = typer.prompt("Project name")
    try:
        name = validate_project_name(name)
    except InvalidProjectName as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    target = (directory / name).resolve()
    try:
        created = scaffold_project(target, name)
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
    typer.echo("  Next:")
    typer.echo(typer.style(f"    cd {name}", fg=typer.colors.BRIGHT_WHITE))
    typer.echo("    # edit secrets/dev.env (set ANTHROPIC_API_KEY) and registry.yml (repos:)")
    typer.echo(
        typer.style("    loopy auth github", fg=typer.colors.BRIGHT_WHITE)
        + typer.style("   # wire git auth (writes loopy.env here)", fg=typer.colors.BRIGHT_BLACK)
    )
    typer.echo(typer.style("    loopy compile . --out manifest.json", fg=typer.colors.BRIGHT_WHITE))
    typer.echo()


@app.command()
def compile(
    path: Path = typer.Argument(Path("."), help="Project directory to compile."),
    out: Path | None = typer.Option(None, "--out", help="Write the manifest JSON to this path."),
) -> None:
    """Compile a project to a validated manifest (and generate loopy.events)."""
    import datetime

    from loopy_core import __version__
    from loopy_core.compile.manifest import to_manifest
    from loopy_core.compile.pipeline import compile_project
    from loopy_core.events.codegen import write_events

    result = compile_project(path)
    for diagnostic in result.diagnostics.items:
        typer.echo(diagnostic.render(), err=True)

    if result.project is not None:
        _print_workflows(result.project)

    if not result.diagnostics.has_errors() and result.project is not None:
        write_events(result.project.registry, path)
        if out is not None:
            manifest = to_manifest(result.project)
            manifest["compiled_at"] = datetime.datetime.now(datetime.UTC).isoformat()
            manifest["loopy_version"] = __version__
            out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    raise typer.Exit(code=result.diagnostics.exit_code())


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


def build_runtime(manifest, *, root: Path, sandbox: str, bus, state=None, tokens=None):
    """Construct the InMemoryRuntime with its standard dependency wiring.

    The single runtime-construction site shared by `run` and `trigger`, so the serve and
    one-shot paths can't drift in how the harness, sandboxes, secrets, bus, durable state,
    and SCM token provider are wired (that drift is exactly what let `trigger` ship without
    token injection). `state` is passed through when given (so a networked bus can share the
    runtime's StateStore); omitted otherwise so the runtime uses its own default.
    """
    from loopy_runtime.harness.router import HarnessRouter
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import make_sandbox_provider
    from loopy_runtime.secrets import CONTROL_PLANE_ENV_FILE, EnvFileSecretsResolver

    extra = {"state": state} if state is not None else {}
    return InMemoryRuntime(
        manifest,
        harness=HarnessRouter(manifest.registry.agents, manifest.registry.events),
        sandboxes=make_sandbox_provider(sandbox),
        secrets=EnvFileSecretsResolver(root),
        bus=bus,
        tokens=tokens,
        github_auth_hint=str(root / CONTROL_PLANE_ENV_FILE),
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

    `--docker` builds the engine image from source, so it needs the build context. Walks up
    from this package; returns None for a wheel install with no source tree alongside it.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _run_in_docker(
    *, root: Path, manifest: Path, port: int | None, sandbox: str, detach: bool, build: bool
) -> None:
    """Bring up the single-node stack (engine + redis) via the bundled compose file.

    The Docker plumbing is an implementation detail: everything compose needs is derived from
    the same flags `loopy run` already takes (`--root`, the manifest, `--port`, `--sandbox`) plus
    the project's `loopy.env` (Daytona creds), and passed through the child's environment — there
    is no user-authored compose file or `.env`.
    """
    import shutil
    import subprocess

    from loopy_runtime.secrets import load_control_plane_env

    if shutil.which("docker") is None:
        typer.echo("error: docker is not installed or not on PATH.", err=True)
        raise typer.Exit(code=1)

    context = _source_root()
    if context is None:
        typer.echo(
            "error: `loopy run --docker` currently requires a source checkout (no pyproject.toml "
            "found alongside the package to build the engine image from).",
            err=True,
        )
        raise typer.Exit(code=1)

    deploy = Path(__file__).resolve().parent / "deploy"
    compose = deploy / "docker-compose.yml"
    dockerfile_rel = os.path.relpath(deploy / "Dockerfile", context)

    root_abs = root.resolve()
    # The container mounts only --root at /project, so the manifest is referenced relative to it.
    # A relative manifest is interpreted against --root (the natural "run from the project" case).
    manifest_abs = manifest if manifest.is_absolute() else (root / manifest)
    manifest_rel = os.path.relpath(manifest_abs.resolve(), root_abs)
    if manifest_rel.startswith(".."):
        typer.echo(
            f"error: manifest {manifest} is outside the project root {root_abs}; "
            "with --docker the manifest must live under --root (it's mounted at /project).",
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
            "LOOPY_BUILD_CONTEXT": str(context),
            "LOOPY_DOCKERFILE": dockerfile_rel,
            "LOOPY_PROJECT": str(root_abs),
            "LOOPY_MANIFEST": Path(manifest_rel).as_posix(),
            # `local` is the in-process default; in a container daytona is the sensible default.
            "LOOPY_SANDBOX": "daytona" if sandbox == "local" else sandbox,
            "LOOPY_PORT": str(port or 8000),
        }
    )

    cmd = ["docker", "compose", "-f", str(compose), "up"]
    if build:
        cmd.append("--build")
    if detach:
        cmd.append("--detach")
    typer.echo(
        f"loopy: bringing up engine + redis via docker (project {root_abs}, "
        f"sandbox {env['LOOPY_SANDBOX']}, port {env['LOOPY_PORT']})"
    )
    raise typer.Exit(code=subprocess.call(cmd, env=env))


@app.command()
def run(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file + sensors)."),
    config: Path = typer.Option(
        Path("loopy.yaml"), "--config", help="Deployment defaults (loopy.yaml); flags override it."
    ),
    host: str | None = typer.Option(None, "--host", help="Override sensor_server.host."),
    port: int | None = typer.Option(None, "--port", help="Override sensor_server.port."),
    sandbox: str = typer.Option(
        "local", "--sandbox", help="Sandbox provider: local | docker | daytona."
    ),
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
    docker: bool = typer.Option(
        False,
        "--docker",
        help="Run the engine + redis as containers (redis bus, sqlite state, daytona sandbox).",
    ),
    detach: bool = typer.Option(
        False, "--detach", "-d", help="With --docker, run the stack in the background."
    ),
    build: bool = typer.Option(
        True, "--build/--no-build", help="With --docker, (re)build the engine image first."
    ),
) -> None:
    """Start the Loopy server: host sensor webhooks; incoming events drive workflow runs."""
    import asyncio

    # `--docker` runs the same single-node composition (this very `run` command, with
    # redis/sqlite/daytona) inside containers. It short-circuits before the in-process
    # wiring below, since that setup is for running the engine in *this* process.
    if docker:
        _run_in_docker(
            root=root, manifest=manifest, port=port, sandbox=sandbox, detach=detach, build=build
        )
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

    m = load_manifest(manifest)
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
            m, root=root, sandbox=sandbox, bus=event_bus, state=state, tokens=tokens
        )
        runtime.preflight()  # fail fast at startup if any sandbox can't supply its harness keys
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    receiver = LocalEventReceiver(event_bus, m.registry.events)  # shared gate for webhooks + polls
    sensor_runner = FastAPISensorRunner(receiver)
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
        sensor_runner.register_webhook(sensor.trigger.path, fn)

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
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    event: str = typer.Option(..., "--event", help="Triggering event name."),
    fields: str | None = typer.Option(None, "--fields", help="Event fields as a JSON object."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
    sandbox: str = typer.Option(
        "local", "--sandbox", help="Sandbox provider: local | docker | daytona."
    ),
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

    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.contract import Event
    from loopy_runtime.manifest_model import load_manifest

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
            m, root=root, sandbox=sandbox, bus=InProcessEventBus(), tokens=tokens
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
