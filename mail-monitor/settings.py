"""Profile-local mail-monitor configuration."""

import json
from pathlib import Path


def home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def settings():
    return json.loads((Path(__file__).parent/'settings.json').read_text())
