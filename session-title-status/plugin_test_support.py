"""Stand-in for the Hermes runtime so the plugin tests run as plain scripts."""

import sys
import tempfile
import types
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


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


class FakeAPI:
    """Synthetic loopback REST: profiles, sources, latest-6 pages and title PATCH."""

    def __init__(self):
        self.data = {'default': {}, 'coding': {}}
        self.messages = {}
        self.calls = []
        self.fail_patch = False

    def add(self, sid, profile='default', title='Plan', source='cli', messages=None):
        self.data[profile][sid] = dict(id=sid, title=title, source=source, archived=False,
                                       hidden=False, is_internal_child=False, parent_session_id=None,
                                       last_active='2026-10-22T00:00:00Z')
        self.messages[(profile, sid)] = list(messages or [{'role': 'user', 'content': 'hello'}])

    def request(self, method, path, body=None, profile='default'):
        self.calls.append((method, path, body, profile))
        parsed = urlparse(path)
        if method == 'GET' and parsed.path == '/api/sessions':
            query = parse_qs(parsed.query)
            source = query.get('source', [''])[0]
            limit = int(query.get('limit', ['50'])[0])
            offset = int(query.get('offset', ['0'])[0])
            rows = [s for s in self.data[profile].values() if s['source'] == source]
            page = rows[offset:offset + limit]
            return {'data': [dict(s) for s in page], 'has_more': offset + limit < len(rows)}
        parts = parsed.path.split('/')
        if method == 'GET' and parsed.path.startswith('/api/sessions/'):
            sid = unquote(parts[3])
            if parsed.path.endswith('/messages'):
                query = parse_qs(parsed.query)
                limit = int(query.get('limit', ['6'])[0])
                data = self.messages.get((profile, sid), [])[-limit:]
                return {'session_id': sid, 'data': [dict(m) for m in reversed(data)]}
            return {'session': dict(self.data[profile][sid])}
        if method == 'PATCH':
            sid = unquote(parts[3])
            if self.fail_patch:
                raise OSError('synthetic PATCH failure')
            self.data[profile][sid]['title'] = body['title']
            return {}
        raise AssertionError((method, path))
