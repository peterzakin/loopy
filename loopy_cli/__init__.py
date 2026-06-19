"""Loopy CLI — one binary over both halves.

    loopy compile   produce the manifest (frontend)
    loopy run       start the server: host sensor webhooks; events drive workflow runs
    loopy trigger   fire one event at the manifest and run it to completion (for testing)

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
    typer.echo(typer.style(f"  🔁  Loopy compiled {count} {plural}", fg=typer.colors.CYAN, bold=True))
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


@app.command()
def run(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file + sensors)."),
    config: Path = typer.Option(
        Path("loopy.yaml"), "--config", help="Deployment defaults (loopy.yaml); flags override it."
    ),
    host: str | None = typer.Option(None, "--host", help="Override sensor_server.host."),
    port: int | None = typer.Option(None, "--port", help="Override sensor_server.port."),
    sandbox: str = typer.Option("local", "--sandbox", help="Sandbox provider: local | daytona."),
    bus: str | None = typer.Option(
        None, "--bus", help="EventBus: inproc | redis. Overrides config."
    ),
    redis_url: str | None = typer.Option(
        None, "--redis-url", help="Redis URL (or REDIS_URL env var; used when bus=redis)."
    ),
) -> None:
    """Start the Loopy server: host sensor webhooks; incoming events drive workflow runs."""
    import asyncio

    from loopy_runtime.bus.factory import make_event_bus
    from loopy_runtime.config import ConfigError, load_config, resolve, resolve_redis_url
    from loopy_runtime.harness.router import HarnessRouter
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.receiver import LocalEventReceiver
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import make_sandbox_provider
    from loopy_runtime.secrets import (
        EnvFileSecretsResolver,
        load_control_plane_env,
        load_sensor_env,
    )
    from loopy_runtime.sensors.loader import load_poll_sensor, load_webhook_sensor
    from loopy_runtime.sensors.runner import FastAPISensorRunner, synthesizing_publisher
    from loopy_runtime.sensors.scheduler import PollScheduler, parse_interval
    from loopy_runtime.state.inmemory import InMemoryStateStore

    # Control-plane infra creds (REDIS_URL, DAYTONA_API_KEY/URL): a local-dev convenience read
    # from `loopy.env`, merged with setdefault (real/platform env always wins). Must land before
    # resolve_redis_url() and any Daytona client creation, both of which read os.environ.
    control_env = load_control_plane_env(root)
    for key, value in control_env.items():
        os.environ.setdefault(key, value)
    if control_env:
        typer.echo(f"loaded {len(control_env)} control-plane var(s) from {root}/loopy.env")

    # SCM token injection: if a GitHub App is configured (via `loopy auth github`), mint a
    # scoped installation token per step and inject it into the sandbox. The App private key
    # stays here at the control-plane; only the ephemeral token crosses into the sandbox.
    from loopy_runtime.scm.github_app import AppCredentials, GitHubAppError
    from loopy_runtime.scm.token_provider import GitHubAppTokenProvider

    tokens = None
    try:
        tokens = GitHubAppTokenProvider(AppCredentials.from_env(os.environ, root=root))
        typer.echo("github app: minting scoped tokens for sandboxes (key stays control-plane)")
    except GitHubAppError:
        pass  # no App configured → no token injection (unchanged behavior)

    try:
        cfg = resolve(load_config(config), host=host, port=port, bus=bus)
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
    state = InMemoryStateStore()
    event_bus = make_event_bus(cfg.bus, redis_url=resolved_redis_url, state=state)
    runtime = InMemoryRuntime(
        m,
        harness=HarnessRouter(m.registry.agents, m.registry.events),
        sandboxes=make_sandbox_provider(sandbox),
        secrets=EnvFileSecretsResolver(root),
        bus=event_bus,
        state=state,
        tokens=tokens,
    )
    try:
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

    asyncio.run(_serve())  # pragma: no cover


@app.command()
def trigger(
    manifest: Path = typer.Argument(..., help="Path to manifest.json."),
    event: str = typer.Option(..., "--event", help="Triggering event name."),
    fields: str | None = typer.Option(None, "--fields", help="Event fields as a JSON object."),
    root: Path = typer.Option(Path("."), "--root", help="Project root (for env_file resolution)."),
    sandbox: str = typer.Option("local", "--sandbox", help="Sandbox provider: local | daytona."),
) -> None:
    """Fire one event at the manifest and run the cascade to completion (for testing)."""
    import asyncio
    from datetime import UTC, datetime

    from loopy_runtime.bus.inproc import InProcessEventBus
    from loopy_runtime.contract import Event
    from loopy_runtime.harness.router import HarnessRouter
    from loopy_runtime.manifest_model import load_manifest
    from loopy_runtime.runtime.inmemory import InMemoryRuntime
    from loopy_runtime.sandbox.factory import make_sandbox_provider
    from loopy_runtime.secrets import EnvFileSecretsResolver

    m = load_manifest(manifest)
    triggering = Event(
        name=event,
        fields=json.loads(fields) if fields else {},
        id="trigger",
        emitted_at=datetime.now(UTC),
    )
    try:
        runtime = InMemoryRuntime(
            m,
            harness=HarnessRouter(m.registry.agents, m.registry.events),
            sandboxes=make_sandbox_provider(sandbox),
            secrets=EnvFileSecretsResolver(root),
            bus=InProcessEventBus(),
        )
        runtime.preflight()  # fail fast before firing the event if any sandbox lacks its keys
        run_id = asyncio.run(runtime.trigger(triggering))
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if run_id is None:
        typer.echo(f"no workflow subscribes to event '{event}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"run: {run_id}")
    typer.echo(f"steps: {' -> '.join(runtime.execution_log)}")
    typer.echo(f"emitted: {', '.join(runtime.emitted_log) or '(none)'}")
    if runtime.failed_runs:  # a recorded run failure → report and exit non-zero
        for status in runtime.failed_runs:
            typer.echo(f"FAILED {status.run_id}: {status.error}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
