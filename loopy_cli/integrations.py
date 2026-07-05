"""`loopy integrations` — see the platform-shipped webhook providers and their status.

`loopy integrations` (or `list`) prints every built-in provider with its webhook path,
whether its signing secret is configured, and how many of its events this project triggers
on. `loopy integrations <name>` (e.g. `sentry`) shows one provider's full event catalog,
its delivery URL (composed from `LOOPY_PUBLIC_URL`), and the exact command to configure it.

Status is read against the project at `--root`: the compiled registry says which built-in
events are referenced, and the merged control-plane env (`loopy.env` under the process env)
says which secrets are set. Heavy imports are deferred into the body so `loopy compile`
stays runtime-free, matching `auth.py`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from loopy_core.builtins import BUILTIN_PROVIDERS, provider_for

# Per-provider command that configures the signing secret (shown when it's unset).
_SECRET_FIX = {
    "github": "loopy webhooks github",
    "sentry": "loopy auth sentry",
    "datadog": "loopy auth datadog",
}


def _merged_env(root: Path) -> dict[str, str]:
    """Process env with `loopy.env` merged underneath (process env wins)."""
    import os

    from loopy_runtime.secrets import load_control_plane_env

    merged = dict(os.environ)
    for key, value in load_control_plane_env(root).items():
        merged.setdefault(key, value)
    return merged


def _referenced_events(root: Path) -> set[str]:
    """The built-in event names this project triggers on (empty if it doesn't compile).

    A built-in event lands in the registry (with `builtin=True`) only when a workflow
    references it, so the compiled registry is the source of truth for "used"."""
    from loopy_core.compile.pipeline import compile_project

    result = compile_project(root)
    if result.project is None:
        return set()
    return {
        name
        for name, event in result.project.registry.events.items()
        if getattr(event, "builtin", False)
    }


def _tick(on: bool) -> str:
    return typer.style("✓", fg=typer.colors.GREEN) if on else typer.style("✗", fg=typer.colors.RED)


def _list_view(root: Path) -> None:
    env = _merged_env(root)
    used = _referenced_events(root)
    typer.echo(typer.style("\n  Integrations", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 48, fg=typer.colors.BRIGHT_BLACK))
    for provider in BUILTIN_PROVIDERS:
        n_used = sum(1 for e in provider.events if e in used)
        secret_set = bool(env.get(provider.secret_env))
        used_note = (
            typer.style(f"{n_used} event{'s' if n_used != 1 else ''}", fg=typer.colors.GREEN)
            if n_used
            else typer.style("unused", fg=typer.colors.BRIGHT_BLACK)
        )
        typer.echo(
            f"  {provider.name:<8} {provider.webhook_path:<16} "
            f"secret {_tick(secret_set)}   {used_note}"
        )
    typer.echo(
        typer.style(
            "\n  loopy integrations <name> for a provider's events and setup.\n",
            fg=typer.colors.BRIGHT_BLACK,
        )
    )


def _detail_view(root: Path, provider) -> None:  # noqa: ANN001 - BuiltinProvider
    from loopy_runtime import config

    env = _merged_env(root)
    used = _referenced_events(root)
    public = config.resolve_public_url(env=env)
    secret_set = bool(env.get(provider.secret_env))

    typer.echo(typer.style(f"\n  {provider.name}", fg=typer.colors.CYAN, bold=True))
    typer.echo(typer.style("  " + "─" * 48, fg=typer.colors.BRIGHT_BLACK))
    typer.echo(f"  webhook path   {provider.webhook_path}")
    if public:
        typer.echo(f"  delivery URL   {config.hook_url(public, provider.webhook_path)}")
    else:
        typer.echo(
            "  delivery URL   "
            + typer.style("set LOOPY_PUBLIC_URL to compute this", fg=typer.colors.BRIGHT_BLACK)
        )
    if secret_set:
        typer.echo(f"  secret         {provider.secret_env}  {_tick(True)} set")
    else:
        fix = _SECRET_FIX.get(provider.name, f"set {provider.secret_env}")
        typer.echo(
            f"  secret         {provider.secret_env}  {_tick(False)} not set  "
            + typer.style(f"({fix})", fg=typer.colors.BRIGHT_BLACK)
        )
    typer.echo("  events")
    for event in provider.events:
        is_used = event in used
        mark = _tick(True) if is_used else " "
        note = typer.style("  (used)", fg=typer.colors.GREEN) if is_used else ""
        typer.echo(f"    {mark} {event}{note}")
    typer.echo()


def run_integrations(name: str | None, root: Path) -> None:
    """List built-in providers, or show one by name. `None`/`"list"` -> the list view."""
    if name is None or name.lower() == "list":
        _list_view(root)
        return
    provider = next((p for p in BUILTIN_PROVIDERS if p.name == name.lower()), None)
    # A `Provider.Event` name is a friendly thing to accept too — resolve it to its provider.
    if provider is None:
        provider = provider_for(name)
    if provider is None:
        known = ", ".join(p.name for p in BUILTIN_PROVIDERS)
        typer.echo(f"error: unknown integration '{name}'; known: {known}", err=True)
        raise typer.Exit(code=1)
    _detail_view(root, provider)


def integrations_command(
    name: str | None = typer.Argument(
        None, help="Integration to show (e.g. sentry). Omit, or 'list', to list all."
    ),
    root: Path = typer.Option(Path("."), "--root", help="Project root (where loopy.env lives)."),
) -> None:
    """Show this project's built-in integrations and whether each is configured."""
    run_integrations(name, root)
