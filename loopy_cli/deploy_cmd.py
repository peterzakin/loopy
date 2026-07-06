"""`loopy deploy` — the shared command group deploy targets register into.

One subcommand per deploy target (see `loopy_cli.deploy_target`): `bootstrap`
(the AWS starter stack, `loopy_cli.bootstrap`) and `render` (Render.com,
`loopy_cli.render`). The group lives in its own module so no provider module
owns it; `loopy_cli/__init__.py` imports each provider module for the side
effect of registering its command.
"""

from __future__ import annotations

import typer

deploy_app = typer.Typer(
    no_args_is_help=True,
    help="Provision hosting for the engine on a named deploy target "
    "(`loopy deploy bootstrap`, `loopy deploy render`).",
)
