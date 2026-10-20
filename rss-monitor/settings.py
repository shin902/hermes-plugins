"""Profile-local RSS monitor configuration."""

import json
from pathlib import Path


def home():
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def config_dir():
    return Path(home()) / 'plugin-config/rss-monitor'


def settings():
    directory = config_dir()
    feeds = json.loads((directory / 'feeds.json').read_text(encoding='utf-8'))
    targets = json.loads((directory / 'delivery.json').read_text(encoding='utf-8'))
    if set(feeds) != set(targets):
        raise ValueError('RSS feeds/delivery jobs differ')
    return feeds, targets
