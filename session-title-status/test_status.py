"""Run: python test_status.py (stdlib, no live session reads/writes)."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location('status_plugin', Path(__file__).with_name('__init__.py'))
# The plugin discovers profiles through the Hermes runtime; stand it in before import.
from plugin_test_support import install_stub
install_stub()
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
# The live list-only kill switch (plugin-data marker) is exercised by test_diagnostic.py.
p.list_only = lambda: False
_tmp = TemporaryDirectory()
p.checkpoint_db_path = lambda: Path(_tmp.name) / 'checkpoints.sqlite3'
base = dict(id='test_session', source='cli', title='★ 手動の題名', archived=False)
assert p.eligible(base)
for source in ('cron', 'api_server', 'telegram', 'discord', 'bot', 'tool'):
    assert not p.eligible(dict(base, source=source))
for key in ('archived', 'hidden', 'is_internal_child'):
    assert not p.eligible(dict(base, **{key: True}))
for title in ('Bot Chat', 'Default session title triage', 'self-triage', 'Default session title rename'):
    assert not p.eligible(dict(base, title=title))
assert p.status_title(base['title'], '👀') == '👀 ★ 手動の題名'
assert p.status_title('⏳ ★ 手動の題名', '✅') == '✅ ★ 手動の題名'
assert p.status_title('✅  spaces ', '🔴') == '🔴  spaces '
assert p.clean_title('  a   b  ') == 'a b'
for bad in ('', '   ', chr(0) + 'a', 'x' + chr(0x200b) + 'y', 'x' * 101, None, 7):
    try:
        p.clean_title(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)

calls = []
state = dict(base)
def fake(method, path, body=None, profile='default'):
    calls.append((method, path, body, profile))
    if method == 'PATCH':
        assert set(body) == {'title'}
        state.update(body)
    if '/messages?' in path:
        return {'session_id': state['id'], 'data': [{'role': 'user', 'content': 'x' * 8000}] * 6}
    return {'session': dict(state)}
p.request = fake
def inspected():
    out = p.perform(dict(operation='inspect', session_id=state['id']))
    return {'evidence_hash': out['evidence_hash'], 'checkpoint_token': out['checkpoint_token']}
conflict = inspected()
assert p.perform(dict(operation='set_status', session_id=state['id'], expected_title='different',
                      status='✅', **conflict)) == {'skipped': 'title conflict'}
assert not any(c[0] == 'PATCH' for c in calls)
args = dict(operation='set_status', session_id=state['id'], expected_title=state['title'],
            status='👀', **inspected())
assert p.perform(args)['updated']
assert calls[-1][0] == 'GET'
assert state['title'] == '👀 ★ 手動の題名'
ack = p.perform(dict(args, expected_title=state['title'], **inspected()))
assert ack['unchanged'] and ack['acknowledged']
# set_title: collapse-and-replace, exactly one title-only PATCH, read back, profile carried through.
out = p.perform(dict(operation='set_title', session_id=state['id'], expected_title=state['title'], title='  新しい   題名 '))
assert out['updated'] and out['before'] == '👀 ★ 手動の題名' and out['title'] == '新しい 題名'
assert calls[-2] == ('PATCH', '/api/sessions/test_session', {'title': '新しい 題名'}, 'default')
assert calls[-1][0] == 'GET'
out = p.perform(dict(operation='set_title', session_id=state['id'], expected_title=state['title'], title='RSS要約の再取得', profile='coding'))
assert out['updated'] and out['profile'] == 'coding'
assert all(c[3] == 'coding' for c in calls[-2:])
assert p.perform(dict(operation='set_title', session_id=state['id'], expected_title='RSS要約の再取得', title='RSS要約の再取得'))['unchanged']
result = p.perform(dict(operation='inspect', session_id=state['id']))
assert len(result['messages']) <= 6
assert sum(len(m['content']) for m in result['messages']) <= 12000
state['archived'] = True
before = sum(c[0] == 'PATCH' for c in calls)
assert p.perform(dict(operation='set_title', session_id=state['id'], expected_title=state['title'], title='新しい題名'))['skipped'] == 'ineligible'
assert sum(c[0] == 'PATCH' for c in calls) == before
state['archived'] = False
for args in ({'operation': 'delete'}, {'operation': 'list', 'profile': 'local'},
             {'operation': 'inspect', 'session_id': '../bad'},
             {'operation': 'set_title', 'session_id': state['id'], 'expected_title': state['title']},
             {'operation': 'set_status', 'session_id': state['id'], 'expected_title': state['title'], 'status': '🤖'}):
    try:
        p.perform(args)
    except ValueError:
        pass
    else:
        raise AssertionError(args)
for title in ('  spaces ', 'two  spaces', 'x' + chr(0x200b) + 'y', 'x' * 100):
    state['title'] = title
    before = sum(c[0] == 'PATCH' for c in calls)
    out = p.perform(dict(operation='set_status', session_id=state['id'], status='👀',
                         expected_title=title, **inspected()))
    assert out['skipped'] == 'API title normalization would alter title'
    assert sum(c[0] == 'PATCH' for c in calls) == before
print('PASS: source/archive/internal/title exclusions, title preservation, conflict skip, title-only PATCH + GET readback, bounded messages, cross-profile prefix, invalid operation/profile/path/args rejection, unsafe title skip')
