"""RSS monitor plugin registration."""

from .cli import register as register_cli
from .rss_publish import register as register_rss_publish


def register(ctx):
    register_rss_publish(ctx)
    register_cli(ctx)
