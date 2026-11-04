"""Native Hermes plugin packaging for the Codex usage operator CLI."""
from . import cli


def register(ctx):
    ctx.register_cli_command(
        name="codex-usage",
        help="Inspect Codex usage and manage resumable queued tasks",
        setup_fn=cli.setup_cli,
        handler_fn=cli.run,
    )
