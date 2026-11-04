"""Stand-in for the Hermes runtime so the plugin tests run as plain scripts."""

import sys
import tempfile
import types
from pathlib import Path


def install_stub():
    """Register a fake hermes_constants with one named profile beside the default home.

    Returns the temporary Hermes root, so callers can add further profiles or files.
    """
    root = Path(tempfile.mkdtemp(prefix='session-title-status-'))
    profile = root / 'profiles/coding'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('', encoding='utf-8')
    sys.modules['hermes_constants'] = types.SimpleNamespace(
        get_default_hermes_root=lambda: root,
        get_hermes_home=lambda: profile,
        profile_name_for_home=lambda path: 'coding',
    )
    return root
