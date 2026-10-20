"""Mail-monitor plugin registration."""

from .cli import register as register_cli
from .mail_publish import register as register_mail_publish


def register(ctx):
    register_mail_publish(ctx)
    register_cli(ctx)
