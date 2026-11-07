"""Run: python test_jev.py (stdlib, synthetic REST + fake Jev transport, no network)."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

from plugin_test_support import FakeAPI, install_stub

# Real PyYAML when present (managed Python), checked once before the stand-in goes in.
try:
    import yaml as real_yaml
except ImportError:
    real_yaml = None

root = install_stub()
(root / 'profiles/local').mkdir()
(root / 'profiles/local/config.yaml').write_text('', encoding='utf-8')
here = Path(__file__).parent
spec = importlib.util.spec_from_file_location('jev_runner_under_test', here / 'jev_runner.py')
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)
P, monitor = jev.PLUGIN, jev.monitor
P.list_only = lambda: False
assert set(P.PROFILES) == {'default', 'coding', 'local'}, P.PROFILES

NOW = 1800000000.0
GROUPS = root / 'plugin-config/bot-acl/groups.yaml'
GOOD_GROUPS = json.dumps({'main': ['default', 'coding'], 'local': ['local']})  # JSON is YAML
# Deterministic stand-in so the partition tests do not depend on PyYAML being installed.
sys.modules['yaml'] = type(sys)('yaml')
sys.modules['yaml'].safe_load = json.loads


def groups(text):
    if text is None:
        GROUPS.unlink(missing_ok=True)
    else:
        GROUPS.parent.mkdir(parents=True, exist_ok=True)
        GROUPS.write_text(text, encoding='utf-8')


def setup(tmp):
    api = FakeAPI()
    api.data['local'] = {}
    P.checkpoint_db_path = lambda: Path(tmp) / 'checkpoints.sqlite3'
    P.request = api.request
    groups(GOOD_GROUPS)
    return api


def reply(choice='review', confidence=0.8):
    probs = {k: 0.025 for k in jev.OPTIONS}
    probs[choice] = 0.9
    return {'model': 'jev-test', 'answers': {jev.QUESTION_ID: {
        'type': 'choice', 'choice': choice, 'confidence': confidence, 'probabilities': probs}}}


class Transport:
    def __init__(self, response=None):
        self.payloads, self.response = [], reply() if response is None else response

    def __call__(self, payload):
        self.payloads.append(payload)
        return self.response(payload) if callable(self.response) else self.response


def go(transport, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    code = jev.run(transport=transport, now=NOW, out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def writes(api):
    return [c for c in api.calls if c[0] == 'PATCH']


def test_partition_is_disjoint_and_complete(tmp):
    api = setup(tmp)
    for profile in ('default', 'coding', 'local'):
        api.add('s_' + profile, profile=profile)
    ids = {}
    for scope in monitor.SCOPES:
        pending, _ = monitor.pending_work(plugin=P, now=NOW, scope=scope)
        ids[scope] = {(x['profile'], x['id']) for x in pending}
    assert ids['local'] == {('local', 's_local')}
    assert ids['cloud'] == {('default', 's_default'), ('coding', 's_coding')}
    assert ids['local'] | ids['cloud'] == ids['all'] and not ids['local'] & ids['cloud']
    # Out-of-scope sessions are dropped before any evidence fetch.
    api.calls.clear()
    monitor.pending_work(plugin=P, now=NOW, scope='cloud')
    assert not any('/messages' in c[1] and c[3] == 'local' for c in api.calls)
    api.calls.clear()
    monitor.pending_work(plugin=P, now=NOW, scope='local')
    assert not any(c[3] != 'local' and '/messages' in c[1] for c in api.calls)
    try:
        monitor.pending_work(plugin=P, now=NOW, scope='bogus')
    except ValueError:
        pass
    else:
        raise AssertionError('unknown scope accepted')


def test_monitor_default_scope_is_local(tmp):
    api = setup(tmp)
    api.add('c1')
    api.add('l1', profile='local')

    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = monitor.main(argv)
        return code, out.getvalue(), err.getvalue()

    assert monitor.pending_work.__defaults__[-1] == 'local'  # (plugin, now, scope)
    code, out, _ = run_main([])
    assert code == 0 and [x['id'] for x in json.loads(out)['pending']] == ['l1'], out
    code, out, _ = run_main(['--scope', 'cloud'])
    assert [x['id'] for x in json.loads(out)['pending']] == ['c1'], out
    code, out, _ = run_main(['--scope', 'all'])
    assert sorted(x['id'] for x in json.loads(out)['pending']) == ['c1', 'l1']
    code, out, err = run_main(['--scope', 'bogus'])
    assert code == 2 and out == '' and 'usage:' in err


def test_local_group_never_reaches_transport(tmp):
    api = setup(tmp)
    api.add('l1', profile='local')
    api.add('l2', profile='local')
    t = Transport()
    code, out, err = go(t)
    assert (code, out, err, t.payloads) == (0, '', '', [])
    # Even when a local-group session is (wrongly) offered as pending, the re-check refuses it.
    wrong = [{'id': 'l1', 'profile': 'local', 'title': 'Plan'}]
    original = monitor.pending_work
    monitor.pending_work = lambda **kw: (wrong, [])
    try:
        for dry_run in (False, True):
            code, out, err = go(t, dry_run=dry_run)
            assert code == 1 and t.payloads == [] and out == '', (dry_run, out, err)
            assert err.startswith('JEV_ERROR local l1 '), err
    finally:
        monitor.pending_work = original
    assert not writes(api) and not (Path(tmp) / 'checkpoints.sqlite3').exists()
    # Calibration mode cannot be pointed at a local-group profile either.
    code, out, err = go(t, dry_run=True, recent=5, profiles=['local'])
    assert (code, out, t.payloads) == (0, '', [])


def test_unknown_groups_fail_closed(tmp):
    api = setup(tmp)
    api.add('c1')
    api.add('c2', profile='coding')
    api.add('l1', profile='local')
    t = Transport()
    broken = [None, '{not json', '[]', '{"main": ["default"]}', '{"local": "local"}',
              '{"local": [1, 2]}', '{"local": null}']
    for text in broken:
        groups(text)
        assert not any(P.is_cloud_allowed(name) for name in P.PROFILES), text
        pending, _ = monitor.pending_work(plugin=P, now=NOW, scope='cloud')
        assert pending == [], text
        everything, _ = monitor.pending_work(plugin=P, now=NOW, scope='local')
        assert len(everything) == 3, text  # all of it stays on the local model
        for kwargs in ({}, {'dry_run': True}, {'dry_run': True, 'recent': 10}):
            assert go(t, **kwargs) == (0, '', ''), (text, kwargs)
    # yaml itself unavailable (ImportError) is the same.
    groups(GOOD_GROUPS)
    assert P.is_cloud_allowed('default')
    saved = sys.modules['yaml']
    sys.modules['yaml'] = None
    try:
        assert not any(P.is_cloud_allowed(name) for name in P.PROFILES)
        assert go(t) == (0, '', '')
    finally:
        sys.modules['yaml'] = saved
    assert t.payloads == [] and not writes(api)
    # The file is read fresh each run: editing it takes effect immediately.
    groups(json.dumps({'local': ['default']}))
    assert not P.is_cloud_allowed('default') and P.is_cloud_allowed('local')


def test_real_yaml_syntax(tmp):
    if real_yaml is None:
        return print('SKIP: PyYAML not installed (managed Python covers it)')
    setup(tmp)
    saved = sys.modules['yaml']
    sys.modules['yaml'] = real_yaml
    try:
        groups('# bot-acl groups\nmain: [default, computer-use, coding]\n'
               'local: [local, logbook, screenpipe, mail, finance, google-calendar]\n')
        assert P.is_cloud_allowed('default') and P.is_cloud_allowed('coding')
        assert not any(P.is_cloud_allowed(n) for n in ('local', 'logbook', 'mail', 'finance'))
        groups('local: [a\n  b')
        assert not P.is_cloud_allowed('default')
    finally:
        sys.modules['yaml'] = saved


def test_happy_path_uses_inspect_hash_and_token(tmp):
    api = setup(tmp)
    api.add('s1', title='🔴 Plan', messages=[{'role': 'user', 'content': 'ship it?'},
                                            {'role': 'assistant', 'content': 'shipped'}])
    api.add('s2', profile='coding', title='Other')
    performed = []
    original = P.perform
    P.perform = lambda args: (performed.append(dict(args)), original(args))[1]
    try:
        t = Transport(reply('done', 0.9))
        code, out, err = go(t)
    finally:
        P.perform = original
    assert code == 0 and err == '' and out == 'coding: 1 changed\ndefault: 1 changed\n', (out, err)
    assert api.data['default']['s1']['title'] == '✅ Plan'
    assert api.data['coding']['s2']['title'] == '✅ Other'
    assert not any(a['operation'] == 'set_title' for a in performed)
    first = [a for a in performed if a.get('session_id') == 's1']
    inspect_args, set_args = first
    seen = original(dict(inspect_args))  # same evidence after the write: hash is the judged one
    assert set_args['operation'] == 'set_status' and set_args['status'] == '✅'
    assert set_args['evidence_hash'] == seen['evidence_hash'] and set_args['checkpoint_token'] == ''
    assert set_args['expected_title'] == '🔴 Plan'
    # Request shape: one choice question, stable ASCII keys, emoji stripped, bounded messages.
    payload = next(x for x in t.payloads if x['state']['title'] == 'Plan')  # s1, emoji stripped
    assert payload['model'] == 'jev-latest' and list(payload['questions']) == [jev.QUESTION_ID]
    question = payload['questions'][jev.QUESTION_ID]
    assert question['type'] == 'choice' and set(question['criteria']) == set(jev.OPTIONS)
    assert all(k.isascii() for k in question['criteria'])
    assert payload['state'] == {'title': 'Plan', 'truncated': False, 'messages': [
        {'role': 'user', 'content': 'ship it?'}, {'role': 'assistant', 'content': 'shipped'}]}
    # Nothing pending now: silent success, no Jev call.
    t2 = Transport()
    assert go(t2) == (0, '', '') and t2.payloads == []
    # Same emoji again after new evidence: acknowledged, but not counted as changed.
    api.messages[('default', 's1')].append({'role': 'user', 'content': 'thanks'})
    assert go(Transport(reply('done', 0.9))) == (0, '', '')


def test_low_confidence_is_unresolved(tmp):
    api = setup(tmp)
    api.add('s1', title='✅ Plan')
    code, out, err = go(Transport(reply('done', 0.1)))
    assert code == 0 and out == 'default: 1 changed\n'
    assert api.data['default']['s1']['title'] == '🔴 Plan'
    assert jev.MIN_CONFIDENCE == 0.30
    emoji, confidence, _, choice = jev.classify({'title': 'x', 'truncated': False, 'messages': []},
                                                Transport(reply('review', 0.30)))
    assert (emoji, choice) == ('👀', 'review')
    emoji, *_ = jev.classify({'title': 'x', 'truncated': False, 'messages': []},
                             Transport(reply('review', 0.29)))
    assert emoji == '🔴'


def test_invalid_response_sets_nothing_and_fails(tmp):
    api = setup(tmp)
    api.add('s1')
    good = reply()
    answer = good['answers'][jev.QUESTION_ID]

    def variant(**changes):
        return {'answers': {jev.QUESTION_ID: dict(answer, **changes)}}

    bad = [{}, {'answers': {}}, {'answers': {jev.QUESTION_ID: None}}, variant(choice='maybe'),
           variant(type='text'), variant(confidence=1.5), variant(confidence=-0.1),
           variant(confidence=float('nan')), variant(confidence='high'), variant(confidence=True),
           variant(probabilities={k: 0.2 for k in list(jev.OPTIONS)[:-1]}),
           variant(probabilities={k: 0.5 for k in jev.OPTIONS}),
           variant(probabilities=dict(answer['probabilities'], done=float('inf'))),
           variant(probabilities=dict(answer['probabilities'], done='x')),
           variant(probabilities=dict(answer['probabilities'], extra=0.0))]
    for response in bad:
        code, out, err = go(Transport(response))
        assert code == 1 and out == '' and err.startswith('JEV_ERROR default s1 JevError'), (response, err)
        assert 'content' not in err and 'hello' not in err
    assert not writes(api) and not (Path(tmp) / 'checkpoints.sqlite3').exists()
    assert api.data['default']['s1']['title'] == 'Plan'

    def boom(payload):
        raise jev.JevError('HTTP 401')
    code, out, err = go(boom)
    assert code == 1 and err == 'JEV_ERROR default s1 JevError\n' and not writes(api)


def test_failure_does_not_block_other_sessions(tmp):
    api = setup(tmp)
    api.add('a_bad')
    api.add('b_ok')

    def flaky(payload):
        if payload['state']['messages'][0]['content'] == 'bad':
            raise jev.JevError('HTTP 500')
        return reply('review', 0.9)
    api.messages[('default', 'a_bad')] = [{'role': 'user', 'content': 'bad'}]
    code, out, err = go(flaky)
    assert code == 1 and out == 'default: 1 changed\n'
    assert err == 'JEV_ERROR default a_bad JevError\n'
    assert api.data['default']['a_bad']['title'] == 'Plan'
    assert api.data['default']['b_ok']['title'] == '👀 Plan'
    # The failed one stays pending; the acknowledged one does not.
    pending, _ = monitor.pending_work(plugin=P, now=NOW, scope='cloud')
    assert [x['id'] for x in pending] == ['a_bad']


def test_normal_non_acknowledgements_are_not_errors(tmp):
    api = setup(tmp)
    api.add('s1')

    def racing(payload):
        # Evidence moves between inspect and set_status -> retry (normal), not an error.
        api.messages[('default', 's1')].append({'role': 'user', 'content': 'more'})
        return reply()
    t = racing
    code, out, err = go(t)
    assert (code, out, err) == (0, '', ''), (out, err)
    assert not writes(api)
    # ... and the next tick offers it again.
    pending, _ = monitor.pending_work(plugin=P, now=NOW, scope='cloud')
    assert [x['id'] for x in pending] == ['s1']


def test_dry_run_writes_nothing(tmp):
    api = setup(tmp)
    api.add('s1', title='🔴 Plan')
    api.add('l1', profile='local')
    t = Transport(reply('done', 0.9))
    code, out, err = go(t, dry_run=True)
    assert code == 0 and err == ''
    line = json.loads(out)
    assert line['profile'] == 'default' and line['session_id'] == 's1' and line['title'] == '🔴 Plan'
    assert line['chosen'] == '✅' and line['confidence'] == 0.9
    assert set(line['probabilities']) == set(jev.OPTIONS)
    assert len(t.payloads) == 1  # the local-group session was not sent
    assert not writes(api) and not (Path(tmp) / 'checkpoints.sqlite3').exists()
    assert api.data['default']['s1']['title'] == '🔴 Plan'
    # Calibration mode ignores checkpoints (acknowledge first), still no writes.
    assert go(t)[0] == 0
    assert monitor.pending_work(plugin=P, now=NOW, scope='cloud')[0] == []
    before = len(writes(api))
    code, out, err = go(t, dry_run=True, recent=5, profiles=['default'])
    assert code == 0 and len(out.splitlines()) == 1 and len(writes(api)) == before
    assert json.loads(out)['title'] == '✅ Plan'


def test_missing_key_fails_before_any_work(tmp):
    api = setup(tmp)
    api.add('s1')
    saved_key, saved_env = jev._jev_key, os.environ.pop('TYPESAFE_API_KEY', None)
    try:
        jev._jev_key = lambda: None
        out, err = io.StringIO(), io.StringIO()
        code = jev.run(now=NOW, out=out, err=err)
        assert code == 1 and out.getvalue() == '' and err.getvalue() == 'JEV_ERROR - - KeyMissing\n'
        assert not writes(api)
        # No pending work: no key needed, silent success.
        api.data['default'].clear()
        assert jev.run(now=NOW, out=out, err=err) == 0
    finally:
        jev._jev_key = saved_key
        if saved_env is not None:
            os.environ['TYPESAFE_API_KEY'] = saved_env


def test_key_lookup_and_http_errors(tmp):
    setup(tmp)
    saved_env = os.environ.pop('TYPESAFE_API_KEY', None)
    env_file = root / '.env'
    try:
        env_file.write_text('OTHER=1\nexport TYPESAFE_API_KEY = "sekret-1"\n', encoding='utf-8')
        assert jev._jev_key() == 'sekret-1'
        os.environ['TYPESAFE_API_KEY'] = 'from-env'
        assert jev._jev_key() == 'from-env'
        del os.environ['TYPESAFE_API_KEY']
        env_file.write_text('TYPESAFE_API_KEY=\n', encoding='utf-8')
        assert jev._jev_key() is None
        env_file.unlink()
        assert jev._jev_key() is None
    finally:
        env_file.unlink(missing_ok=True)
        if saved_env is not None:
            os.environ['TYPESAFE_API_KEY'] = saved_env
    # Transport errors surface as JevError without the key or body.
    saved = jev.urllib.request.urlopen

    def deny(req, timeout=None):
        assert req.headers['Authorization'] == 'Bearer sekret-2' and timeout == 30
        raise urllib.error.HTTPError(req.full_url, 401, 'nope', {}, io.BytesIO(b'{"detail":"sekret-2"}'))
    jev.urllib.request.urlopen = deny
    try:
        jev.post_jev({'x': 1}, 'sekret-2')
    except jev.JevError as exc:
        assert str(exc) == 'HTTP 401' and 'sekret' not in str(exc)
    else:
        raise AssertionError('401 accepted')
    finally:
        jev.urllib.request.urlopen = saved


def test_cli_validation():
    for argv in (['--recent', '5'], ['--recent', '0', '--dry-run'], ['--profile', 'nope']):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                jev.main(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:
            raise AssertionError(argv)


def run():
    tests = [test_partition_is_disjoint_and_complete, test_monitor_default_scope_is_local,
             test_local_group_never_reaches_transport, test_unknown_groups_fail_closed,
             test_real_yaml_syntax, test_happy_path_uses_inspect_hash_and_token,
             test_low_confidence_is_unresolved, test_invalid_response_sets_nothing_and_fails,
             test_failure_does_not_block_other_sessions, test_normal_non_acknowledgements_are_not_errors,
             test_dry_run_writes_nothing, test_missing_key_fails_before_any_work,
             test_key_lookup_and_http_errors]
    for test in tests:
        with TemporaryDirectory() as tmp:
            test(tmp)
        print('PASS:', test.__name__)
    test_cli_validation()
    print('PASS: scope partition, local-group never sent (also dry-run), fail-closed groups.yaml, '
          'strict response validation, low confidence -> red, inspect hash/token CAS, '
          'error isolation, dry-run no writes, key lookup, CLI validation')


if __name__ == '__main__':
    run()
