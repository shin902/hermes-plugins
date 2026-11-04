"""Run: python test_diagnostic.py; no REST or provider calls."""
import copy
import importlib.util
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

spec = importlib.util.spec_from_file_location('status_plugin', Path(__file__).with_name('__init__.py'))
# The plugin discovers profiles through the Hermes runtime; stand it in before import.
from plugin_test_support import install_stub
install_stub()
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
with TemporaryDirectory() as tmp:
    root = Path(tmp) / 'plugin-data/session-title-status'
    root.mkdir(parents=True)
    p.diagnostic_dir = lambda: root
    log = root / 'default-api-request-tools.jsonl'
    for sid in (None, '', 'cli_1', 'bot_1', 'cron', 123):
        assert p.observe_api_request(request={'body': {'SECRET': 'BODY'}}, session_id=sid) is None
    assert not log.exists()
    tools = [{'type': 'function', 'function': {'name': 'default_session_status', 'parameters': {'SECRET': 'SCHEMA'}}}, {'type': 'function', 'name': 'other'}]
    requests = [{'tools': tools}, {'body': {'tools': tools}}, {'body': json.dumps({'tools': tools})}, json.dumps({'body': {'tools': tools}}), NS(body=NS(tools=[NS(function=NS(name='default_session_status')), NS(name='other')]))]
    for req in requests:
        before = copy.deepcopy(req)
        assert p.observe_api_request(request=req, model='test-model', api_mode='responses', session_id='cron_test_run', messages='SECRET', title='SECRET', arguments='SECRET', credentials='SECRET') is None
        assert req == before if not isinstance(req, NS) else vars(req.body) == vars(before.body)
    rows = [json.loads(s) for s in log.read_text().splitlines()]
    assert len(rows) == len(requests)
    for r in rows:
        assert set(r) == {'timestamp', 'session_id', 'model', 'api_mode', 'tool_count', 'tool_names'}
        assert datetime.fromisoformat(r.pop('timestamp')).utcoffset() == timezone.utc.utcoffset(None)
        assert r == dict(session_id='cron_test_run', model='test-model', api_mode='responses', tool_count=2, tool_names=['default_session_status', 'other'])
    assert 'SECRET' not in log.read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    for req, count in [({'body': {}}, 0), ({'body': {'tools': []}}, 0), ({'_truncated': True, 'preview': 'SECRET'}, None), ('bad json', None), ({'body': {'tools': [{'_truncated_items': 3}]}}, None)]:
        p.observe_api_request(request=req, session_id='cron_test_run')
        assert json.loads(log.read_text().splitlines()[-1])['tool_count'] == count
    log.unlink()
    target = root / 'untouched'
    target.write_text('unchanged')
    log.symlink_to(target)
    p.observe_api_request(request={'tools': tools}, session_id='cron_test_run')
    assert target.read_text() == 'unchanged'
    assert not p.list_only()
    marker = root / 'default-list-only'
    marker.touch()
    def no_rest(*a, **kw):
        raise AssertionError('REST must not be reached')
    p.request = no_rest
    for op in ('inspect', 'set_status', 'set_title', 'delete', None):
        try:
            p.perform({'operation': op})
        except ValueError as exc:
            assert str(exc) == 'List-only diagnostic mode'
        else:
            raise AssertionError(op)
    calls = []
    def spy(method, path, body=None, profile='default'):
        calls.append((method, path, body, profile))
        return {'data': [], 'has_more': False}
    p.request = spy
    assert p.perform({'operation': 'list'})['sessions'] == []
    # Unscoped list covers every allowlisted profile x every eligible source.
    assert len(calls) == 3 * len(p.PROFILES) and all(c[0] == 'GET' for c in calls)
    assert {c[3] for c in calls} == set(p.PROFILES)
    marker.unlink()
    marker.symlink_to(root / 'missing')
    assert p.list_only()
    class Context:
        def register_hook(self, name, callback):
            assert name == 'pre_api_request' and callback == p.observe_api_request
        def register_tool(self, **kw):
            assert kw['name'] == 'default_session_status'
        def register_cli_command(self, **kw):
            pass
    p.register(Context())
print('PASS: cron-only filter, UTC timestamp/session_id six-field allowlist, hook registration, request shapes, no mutation, redaction, 0600, symlink refusal, truncated/unknown payloads, fail-closed marker before REST, list permitted')
