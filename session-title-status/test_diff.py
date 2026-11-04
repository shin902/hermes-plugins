"""Run: python test_diff.py (stdlib, synthetic REST + temp SQLite, no Hermes)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, unquote, urlparse

spec = importlib.util.spec_from_file_location('status_plugin', Path(__file__).with_name('__init__.py'))
# The plugin discovers profiles through the Hermes runtime; stand it in before import.
from plugin_test_support import install_stub
install_stub()
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
p.list_only = lambda: False

mspec = importlib.util.spec_from_file_location('status_monitor', Path(__file__).with_name('monitor_script.py'))
monitor = importlib.util.module_from_spec(mspec)
mspec.loader.exec_module(monitor)


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


def setup(tmp):
    api = FakeAPI()
    p.checkpoint_db_path = lambda: Path(tmp) / 'checkpoints.sqlite3'
    p.request = api.request
    monitor.PLUGIN = p
    return api


def render():
    return monitor.render(plugin=p)


def inspect(sid='s1', profile='default'):
    return p.perform({'operation': 'inspect', 'profile': profile, 'session_id': sid})


def ack(sid='s1', profile='default', status='👀', expected_title=None, token=None, evidence=None):
    seen = inspect(sid, profile)
    return p.perform({'operation': 'set_status', 'profile': profile, 'session_id': sid,
                      'expected_title': seen['title'] if expected_title is None else expected_title,
                      'status': status,
                      'evidence_hash': seen['evidence_hash'] if evidence is None else evidence,
                      'checkpoint_token': seen['checkpoint_token'] if token is None else token})


def pending():
    output = render()
    work = json.loads(output)
    assert work['wakeAgent'] is True, output
    return work


def run_main(argv=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = monitor.main([] if argv is None else argv)
    return code, out.getvalue(), err.getvalue()


def test_unchanged_and_own_emoji(tmp):
    api = setup(tmp)
    api.add('s1', messages=[{'role': 'user', 'content': 'hello'}])
    work = pending()
    assert [x['id'] for x in work['pending']] == ['s1']
    assert work['pending'][0]['reason'] == 'new'
    assert work['pending'][0]['checkpoint_token'] == ''
    seen = inspect()
    assert not (Path(tmp) / 'checkpoints.sqlite3').exists()  # inspect is read-only
    assert seen['checkpoint_token'] == '' and len(seen['evidence_hash']) == 64
    out = ack(status='👀')
    assert out['acknowledged'] and out['updated']
    assert api.data['default']['s1']['title'] == '👀 Plan'
    assert render() == '{"wakeAgent":false}\n'
    # Idempotent same-status acknowledgement: no PATCH, checkpoint advances.
    patches = sum(c[0] == 'PATCH' for c in api.calls)
    out = ack(status='👀')
    assert out['acknowledged'] and out['unchanged']
    assert sum(c[0] == 'PATCH' for c in api.calls) == patches
    # A title/edit (including our own emoji) is not evidence.
    api.data['default']['s1']['title'] = '⏳ Plan'
    assert render() == '{"wakeAgent":false}\n'
    # New and changed sessions are the only deltas.
    api.add('s2')
    api.messages[('default', 's1')].append({'role': 'assistant', 'content': 'new'})
    work = pending()
    assert [(x['id'], x['reason']) for x in work['pending']] == [('s1', 'evidence_changed'), ('s2', 'new')]
    assert ack('s2', status='✅')['updated']
    assert ack('s1', status='🔴')['updated']
    assert render() == '{"wakeAgent":false}\n'


def test_wake_gate_json_and_retry(tmp):
    api = setup(tmp)
    api.add('s1')
    code, stdout, stderr = run_main()
    assert code == 0 and stderr == ''
    first = json.loads(stdout)
    assert first['wakeAgent'] is True and [x['id'] for x in first['pending']] == ['s1']
    # No cycle/timestamp tricks: an unacknowledged pending set is byte-identical
    # and still wakes the model on the next scheduled run.
    code, stdout2, stderr = run_main()
    assert (code, stdout2, stderr) == (0, stdout, '')
    assert 'cycle' not in first and 'timestamp' not in first
    assert ack()['updated']
    code, stdout, stderr = run_main()
    assert (code, stdout, stderr) == (0, '{"wakeAgent":false}\n', '')
    # Arguments are not supported; still no successful JSON.
    code, stdout, stderr = run_main(['--unexpected'])
    assert code == 2 and stdout == '' and 'usage:' in stderr


def test_gate_error_nonzero_without_payload(tmp):
    api = setup(tmp)
    api.add('s1')
    (Path(tmp) / 'checkpoints.sqlite3').write_bytes(b'not a sqlite database')
    api.calls.clear()
    try:
        render()
    except monitor.MonitorError as exc:
        assert str(exc) == 'checkpoint_db'
    else:
        raise AssertionError('corrupt DB must fail closed')
    assert api.calls == []  # never becomes an empty/full scan
    code, stdout, stderr = run_main()
    assert code == 2
    assert stdout == ''  # no partial successful payload
    assert stderr == 'MONITOR_ERROR checkpoint_db\n'
    # A REST failure must also fail closed with no JSON on stdout.
    (Path(tmp) / 'checkpoints.sqlite3').unlink()
    original = api.request

    def fail_list(method, path, body=None, profile='default'):
        if method == 'GET' and path.startswith('/api/sessions?'):
            raise OSError('synthetic list failure')
        return original(method, path, body, profile)

    api.request = fail_list
    p.request = fail_list
    code, stdout, stderr = run_main()
    assert code == 2 and stdout == '' and stderr == 'MONITOR_ERROR rest_list\n'
    api.request = original
    p.request = original


def test_failed_work_retries(tmp):
    api = setup(tmp)
    api.add('s1')
    first = render()
    assert json.loads(first)['wakeAgent'] is True
    # No acknowledgement (classification failure) leaves the row untouched.
    assert p.read_all_checkpoints() == {}
    second = render()
    assert second == first  # no changing cycle field is needed to retrigger
    assert not (Path(tmp) / 'checkpoints.sqlite3').exists()  # gate is read-only
    # A failed write is also unacknowledged.
    api.fail_patch = True
    seen = inspect()
    try:
        p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': seen['title'],
                   'status': '👀', 'evidence_hash': seen['evidence_hash'],
                   'checkpoint_token': seen['checkpoint_token']})
    except OSError:
        pass
    else:
        raise AssertionError('PATCH failure must propagate')
    assert p.read_all_checkpoints() == {}
    assert json.loads(render())['wakeAgent'] is True
    api.fail_patch = False
    assert ack()['updated']
    assert render() == '{"wakeAgent":false}\n'


def test_title_conflict_and_evidence_change(tmp):
    api = setup(tmp)
    api.add('s1')
    seen = inspect()
    api.data['default']['s1']['title'] = 'Human rename'
    out = ack(status='👀', expected_title=seen['title'])
    assert out == {'skipped': 'title conflict'}
    assert p.read_all_checkpoints() == {}
    assert sum(c[0] == 'PATCH' for c in api.calls) == 0
    api.data['default']['s1']['title'] = seen['title']
    assert ack(status='👀')['updated']
    committed = p.read_all_checkpoints()[('default', 's1')]
    seen = inspect()
    api.messages[('default', 's1')].append({'role': 'assistant', 'content': 'moved'})
    patches = sum(c[0] == 'PATCH' for c in api.calls)
    out = p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': seen['title'],
                     'status': '✅', 'evidence_hash': seen['evidence_hash'],
                     'checkpoint_token': seen['checkpoint_token']})
    assert out['retry'] == 'evidence changed' and out['evidence_hash'] != seen['evidence_hash']
    assert p.read_all_checkpoints()[('default', 's1')] == committed
    assert sum(c[0] == 'PATCH' for c in api.calls) == patches
    assert pending()['pending'][0]['reason'] == 'evidence_changed'


def test_checkpoint_conflict_and_idempotent_retry(tmp):
    api = setup(tmp)
    api.add('s1', title='👀 Plan')
    first = inspect()
    stale = inspect()
    assert first['checkpoint_token'] == stale['checkpoint_token'] == ''
    assert ack(status='👀')['unchanged']
    before = p.read_all_checkpoints()[('default', 's1')]
    patches = sum(c[0] == 'PATCH' for c in api.calls)
    out = p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': '👀 Plan',
                     'status': '✅', 'evidence_hash': stale['evidence_hash'],
                     'checkpoint_token': stale['checkpoint_token']})
    assert out == {'skipped': 'checkpoint conflict', 'id': 's1', 'profile': 'default'}
    assert p.read_all_checkpoints()[('default', 's1')] == before
    assert api.data['default']['s1']['title'] == '👀 Plan'
    assert sum(c[0] == 'PATCH' for c in api.calls) == patches
    # Replaying the same successful result with the old token is acknowledged
    # without rewriting the newer checkpoint.
    out = p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': '👀 Plan',
                     'status': '👀', 'evidence_hash': stale['evidence_hash'],
                     'checkpoint_token': stale['checkpoint_token']})
    assert out['acknowledged'] and out['unchanged'] and out['idempotent']
    assert p.read_all_checkpoints()[('default', 's1')] == before
    assert sum(c[0] == 'PATCH' for c in api.calls) == patches
    # If the human removed the state emoji, the old token must not be claimed as
    # an unchanged success: the title no longer reflects the checkpoint status.
    api.data['default']['s1']['title'] = 'Plan'
    out = p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': 'Plan',
                     'status': '👀', 'evidence_hash': stale['evidence_hash'],
                     'checkpoint_token': stale['checkpoint_token']})
    assert out == {'skipped': 'checkpoint conflict', 'id': 's1', 'profile': 'default'}
    assert api.data['default']['s1']['title'] == 'Plan'


def test_checkpoint_token_covers_whole_row(tmp):
    api = setup(tmp)
    api.add('s1')
    assert p.checkpoint_token(None) == ''
    base = {'profile': 'default', 'session_id': 's1', 'evidence_hash': 'a' * 64,
            'status': '👀', 'classifier_version': 1, 'checked_at': '2026-10-22T00:00:00+00:00'}
    assert p.checkpoint_token(base) != p.checkpoint_token(dict(base, status='✅'))
    assert p.checkpoint_token(base) != p.checkpoint_token(dict(base, classifier_version=2))
    assert p.checkpoint_token(base) != p.checkpoint_token(dict(base, checked_at='2026-10-22T00:00:01+00:00'))
    assert p.checkpoint_token(base) != p.checkpoint_token(dict(base, evidence_hash='b' * 64))
    assert len(p.checkpoint_token(base)) == 64

    assert ack(status='👀')['updated']
    row1 = p.read_checkpoint('default', 's1')
    token1 = p.checkpoint_token(row1)
    seen = inspect()
    assert seen['evidence_hash'] == row1['evidence_hash']
    assert seen['checkpoint_token'] == token1
    # Same evidence hash, different classification: token differs and the stale
    # token cannot overwrite the newer row.
    assert ack(status='✅', token=seen['checkpoint_token'],
               evidence=seen['evidence_hash'])['updated']
    row2 = p.read_checkpoint('default', 's1')
    token2 = p.checkpoint_token(row2)
    assert row2['evidence_hash'] == row1['evidence_hash']
    assert row2['status'] != row1['status']
    assert token2 != token1
    patches = sum(c[0] == 'PATCH' for c in api.calls)
    out = p.perform({'operation': 'set_status', 'session_id': 's1', 'expected_title': '✅ Plan',
                     'status': '👀', 'evidence_hash': row2['evidence_hash'],
                     'checkpoint_token': token1})
    assert out == {'skipped': 'checkpoint conflict', 'id': 's1', 'profile': 'default'}
    assert p.read_checkpoint('default', 's1') == row2
    assert sum(c[0] == 'PATCH' for c in api.calls) == patches
    # Store CAS rejects a stale token even when evidence hash and status match
    # only the token's older row.
    try:
        p.save_checkpoint('default', 's1', row2['evidence_hash'], row2['status'], token1)
    except p.CheckpointConflict:
        pass
    else:
        raise AssertionError('stale token must not pass store CAS')
    assert p.read_checkpoint('default', 's1') == row2


def test_profile_isolation(tmp):
    api = setup(tmp)
    api.add('shared', profile='default')
    api.add('shared', profile='coding')
    assert ack('shared', 'default', '👀')['updated']
    work = pending()
    assert [(x['profile'], x['id'], x['reason']) for x in work['pending']] == [('coding', 'shared', 'new')]
    assert ack('shared', 'coding', '✅')['updated']
    assert render() == '{"wakeAgent":false}\n'
    assert set(p.read_all_checkpoints()) == {('default', 'shared'), ('coding', 'shared')}


def test_hash_full_content_and_latest6(tmp):
    api = setup(tmp)
    messages = [{'role': 'user' if i % 2 else 'assistant', 'content': f'm{i}'} for i in range(8)]
    api.add('deep', messages=messages)
    first = inspect('deep')
    assert len(first['messages']) == 6
    assert sum(len(m['content']) for m in first['messages']) <= 12000
    api.messages[('default', 'deep')][0]['content'] = 'oldest changed'
    assert inspect('deep')['evidence_hash'] == first['evidence_hash']  # outside latest 6
    api.messages[('default', 'deep')][-1]['content'] = 'newest changed'
    assert inspect('deep')['evidence_hash'] != first['evidence_hash']

    api.add('long', messages=[{'role': 'user', 'content': 'A' * 5000 + 'tail1'},
                              {'role': 'assistant', 'content': 'B' * 5000}])
    a = inspect('long')
    assert a['truncated'] and all(len(m['content']) <= 4000 for m in a['messages'])
    assert sum(len(m['content']) for m in a['messages']) <= 12000
    api.messages[('default', 'long')][0]['content'] = 'A' * 5000 + 'tail2'
    b = inspect('long')
    assert b['evidence_hash'] != a['evidence_hash']
    assert b['messages'] == a['messages']  # identical bounded view, different full hash

    api.add('structured', messages=[{'role': 'user', 'content': {'b': [2], 'a': 1}}])
    h1 = inspect('structured')['evidence_hash']
    api.messages[('default', 'structured')] = [{'role': 'user', 'content': {'a': 1, 'b': [2]}}]
    assert inspect('structured')['evidence_hash'] == h1


def test_pagination(tmp):
    api = setup(tmp)
    for i in range(55):
        api.add('s%03d' % i, source='cli', title='P%d' % i)
    work = pending()
    assert len(work['pending']) == 55
    list_calls = [c[1] for c in api.calls if c[0] == 'GET' and '/api/sessions?' in c[1]
                  and 'source=cli' in c[1] and c[3] == 'default']
    assert any('offset=50' in path for path in list_calls)
    assert len(list_calls) == 2


def test_classifier_version_forces_reclassification(tmp):
    api = setup(tmp)
    api.add('s1')
    assert ack()['updated']
    assert render() == '{"wakeAgent":false}\n'
    old = p.CLASSIFIER_VERSION
    try:
        p.CLASSIFIER_VERSION = old + 1
        work = pending()
        assert work['pending'][0]['reason'] == 'classifier_version'
        assert ack()['unchanged']  # same status still acknowledges the new version
        assert render() == '{"wakeAgent":false}\n'
    finally:
        p.CLASSIFIER_VERSION = old


def test_acknowledgement_credentials_required(tmp):
    api = setup(tmp)
    api.add('s1')
    for args in ({'operation': 'set_status', 'session_id': 's1', 'expected_title': 'x', 'status': '👀'},
                 {'operation': 'set_status', 'session_id': 's1', 'expected_title': 'x', 'status': '👀',
                  'evidence_hash': 'x' * 64, 'checkpoint_token': ''},
                 {'operation': 'set_status', 'session_id': 's1', 'expected_title': 'x', 'status': '👀',
                  'evidence_hash': 'a' * 64, 'checkpoint_token': 'bad'},
                 {'operation': 'set_status', 'session_id': 's1', 'expected_title': 'x', 'status': '🤖',
                  'evidence_hash': 'a' * 64, 'checkpoint_token': ''}):
        try:
            p.perform(args)
        except ValueError:
            pass
        else:
            raise AssertionError(args)


def run():
    tests = [test_unchanged_and_own_emoji, test_wake_gate_json_and_retry,
             test_gate_error_nonzero_without_payload, test_failed_work_retries,
             test_title_conflict_and_evidence_change, test_checkpoint_conflict_and_idempotent_retry,
             test_checkpoint_token_covers_whole_row, test_profile_isolation,
             test_hash_full_content_and_latest6, test_pagination,
             test_classifier_version_forces_reclassification, test_acknowledgement_credentials_required]
    for test in tests:
        with TemporaryDirectory() as tmp:
            test(tmp)
        print('PASS:', test.__name__)
    print('PASS: differential gate, pending/no-work JSON, retry, fail-closed error, title conflict, '
          'full-row checkpoint CAS/idempotency, profile isolation, full-content hash, pagination, '
          'version bump, required credentials')


if __name__ == '__main__':
    run()
