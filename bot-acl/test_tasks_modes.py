"""bot_task spawn modes, unlimited runtime and ownership regressions (no model calls)."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_acl import acl
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

tasks = sys.modules[acl.__name__ + '.bot_task']


class FakeChild:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.sid = None
        self.session_key = None
        self.reply = ""
        self.error = None
        self.outcome = None
        self.closed = False
        self.ready = threading.Event()
        self.ready.set()
        self.done = threading.Event()
        self.proc = Mock()
        self.proc.poll.return_value = None
        self.close_calls = 0

    def rpc(self, method, params):
        self.calls.append((method, params))
        value = self.responses.get(method, {})
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.close_calls += 1
        self.closed = True


class StartModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config.yaml').write_text('{}\n')
        (self.root / 'profile.yaml').write_text('{}\n')
        for name in ('coding', 'mail'):
            home = self.root / 'profiles' / name
            home.mkdir(parents=True)
            (home / 'profile.yaml').write_text('{}\n')
        token = set_hermes_home_override(self.root / 'profiles/coding')
        self.addCleanup(reset_hermes_home_override, token)
        self.patchers = [
            patch.object(tasks.ask_bot, 'identity', return_value=('coding', self.root)),
            patch.object(tasks.ask_bot, 'get_hermes_home', return_value=str(self.root / 'profiles/coding')),
            patch.object(tasks, 'check', return_value=None),
            patch.dict(tasks.os.environ, {tasks.ask_bot.DEPTH_ENV: '0'}),
            patch.object(tasks.importlib.util, 'find_spec',
                         return_value=Mock(origin=str(self.root / 'tui_gateway' / '__init__.py'))),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = tasks._TASKS.copy()
        tasks._TASKS.clear()
        self.addCleanup(lambda: (tasks._TASKS.clear(), tasks._TASKS.update(self.saved)))

    def new_task(self, mode='new_session', requested=None):
        task = {'id': 't', 'owner': (str(self.root / 'profiles/coding'), 'parent'),
                'target': 'mail', 'mode': mode,
                'requested_session_id': requested if mode == 'resume' else None,
                'status': 'starting', 'child': None,
                'lock': threading.RLock()}
        tasks._TASKS['t'] = task
        return task

    def start(self, fake, mode='new_session', requested=None):
        task = self.new_task(mode, requested)
        with patch.object(tasks, 'Child', return_value=fake):
            tasks._start(task, 'mail', 'hello', mode, requested)
        return task

    def call(self, args, parent='parent'):
        return json.loads(tasks.handle({}, args, session_id=parent))

    def test_new_session_mode_creates_only(self):
        fake = FakeChild({
            'session.create': {'session_id': 'runtime', 'stored_session_id': 'stored'},
            'prompt.submit': {},
        })
        task = self.start(fake)
        self.assertEqual(task['status'], 'running')
        self.assertEqual([m for m, _ in fake.calls], ['client.capabilities', 'session.create', 'prompt.submit'])
        self.assertEqual(fake.calls[-1][1], {'session_id': 'runtime', 'text': 'hello'})
        snapshot = tasks._snapshot(task)
        self.assertEqual(snapshot['mode'], 'new_session')
        self.assertEqual(snapshot['session_id'], 'runtime')
        self.assertEqual(snapshot['stored_session_id'], 'stored')
        self.assertFalse(snapshot['finality_verified'])
        self.assertNotIn('timeout_seconds', snapshot)
        self.assertNotIn('deadline_seconds', snapshot)

    def test_resume_uses_exact_target_session(self):
        # No local state.db exists: the target-scoped RPC is the only validator.
        self.assertFalse((self.root / 'profiles/mail/state.db').exists())
        fake = FakeChild({
            'session.resume': {'session_id': 'runtime', 'resumed': 'target-session'},
            'prompt.submit': {},
        })
        task = self.start(fake, mode='resume', requested='target-session')
        self.assertEqual(task['status'], 'running')
        methods = [m for m, _ in fake.calls]
        self.assertNotIn('session.create', methods)
        self.assertNotIn('session.list', methods)
        self.assertEqual(methods.count('session.resume'), 1)
        self.assertEqual(fake.calls[1][1]['session_id'], 'target-session')
        self.assertEqual(tasks._snapshot(task)['requested_session_id'], 'target-session')

    def test_resume_not_found_refusal_neither_prompts_nor_retries(self):
        fake = FakeChild({
            'session.resume': tasks.RPCError('session not found', code=4007),
        })
        task = self.start(fake, mode='resume', requested='valid-looking-session')
        self.assertEqual(task['status'], 'failed_unknown')
        methods = [m for m, _ in fake.calls]
        self.assertEqual(methods.count('session.resume'), 1)
        self.assertEqual(methods.count('session.create'), 0)
        self.assertNotIn('prompt.submit', methods)
        self.assertTrue(fake.closed)

    def test_resume_ownership_refusal_is_target_busy_without_prompt_or_retry(self):
        fake = FakeChild({
            'session.resume': tasks.RPCError('recipient refused', code=4090,
                                             data={'reason': 'SESSION_NOT_OWNED'}),
        })
        task = self.start(fake, mode='resume', requested='valid-looking-session')
        self.assertEqual(task['status'], 'target_busy')
        self.assertIn('resend', task['next_step'].lower())
        methods = [m for m, _ in fake.calls]
        self.assertEqual(methods.count('session.resume'), 1)
        self.assertEqual(methods.count('session.create'), 0)
        self.assertNotIn('prompt.submit', methods)
        self.assertTrue(fake.closed)

    def test_ownership_refusal_is_target_busy_without_resend(self):
        fake = FakeChild({
            'session.create': {'session_id': 'runtime', 'stored_session_id': 'stored'},
            'prompt.submit': tasks.RPCError('recipient refused',
                code=4090, data={'reason': 'SESSION_NOT_OWNED'}),
        })
        task = self.start(fake)
        self.assertEqual(task['status'], 'target_busy')
        self.assertIn('resend', task['next_step'].lower())
        self.assertEqual([m for m, _ in fake.calls].count('prompt.submit'), 1)
        self.assertEqual([m for m, _ in fake.calls].count('session.create'), 1)

    def test_startup_readiness_remains_bounded_without_task_timer(self):
        fake = FakeChild({'session.create': {'session_id': 'runtime'}, 'prompt.submit': {}})
        fake.waited = None
        fake.ready = Mock()

        def wait(timeout):
            fake.waited = timeout
            return True
        fake.ready.wait = wait
        self.start(fake)
        self.assertEqual(fake.waited, 30)
        self.assertFalse(hasattr(fake, 'timer'))


class HandleValidationTests(unittest.TestCase):
    setUp = StartModeTests.setUp
    new_task = StartModeTests.new_task
    start = StartModeTests.start
    call = StartModeTests.call

    def test_resume_defers_existence_to_target_scoped_rpc(self):
        # A valid-looking ID is admitted and handed to the child even though the
        # target profile has no local state.db to probe.
        self.assertFalse((self.root / 'profiles/mail/state.db').exists())
        started = threading.Event()
        forwarded = []

        def start(*args):
            forwarded.append(args)
            started.set()

        with patch.object(tasks, '_start', side_effect=start):
            result = self.call({'action': 'spawn', 'target': 'mail', 'message': 'hello',
                                'mode': 'resume', 'session_id': 'valid-looking-session'})
            self.assertEqual(result['status'], 'starting')
            self.assertTrue(started.wait(1))
        self.assertEqual(len(forwarded), 1)
        _, target, text, mode, requested = forwarded[0]
        self.assertEqual((target, text, mode, requested),
                         ('mail', 'hello', 'resume', 'valid-looking-session'))

    def test_resume_rejects_malformed_ids_before_spawn(self):
        for sid in ('latest', 'LATEST', '--provider=evil', '../state', 'x;false',
                    '', '   ', 'sess/child', 'a' * 129, None):
            with patch.object(tasks, '_start') as start:
                result = self.call({'action': 'spawn', 'target': 'mail', 'message': 'hello',
                                    'mode': 'resume', 'session_id': sid})
                self.assertEqual(result['status'], 'failed', sid)
                start.assert_not_called()

    def test_session_id_only_with_resume(self):
        for mode in ('new_session', 'bot_chat'):
            with patch.object(tasks, '_start') as start:
                result = self.call({'action': 'spawn', 'target': 'mail', 'message': 'hello',
                                    'mode': mode, 'session_id': 'target-session'})
            self.assertEqual(result['status'], 'failed')
            start.assert_not_called()

    def test_mode_validation_and_timeout_rejection(self):
        with patch.object(tasks, '_start'):
            for mode in ('unknown', '', None, 'bot_chat'):
                self.assertEqual(self.call({'action': 'spawn', 'target': 'mail',
                                            'message': 'hello', 'mode': mode})['status'], 'failed')
        for timeout in (0, 30, 600, -1, 1, 29, 601, True, 1.5):
            with patch.object(tasks, '_start') as start:
                result = self.call({'action': 'spawn', 'target': 'mail', 'message': 'hello',
                                    'timeout_seconds': timeout})
                self.assertEqual(result['status'], 'failed', timeout)
                self.assertIn('timeout_seconds is not supported', result['error'])
                start.assert_not_called()

    def test_parent_and_resume_session_ids_stay_separate(self):
        entered = threading.Event()
        def start(*args):
            entered.set()
        with patch.object(tasks, '_start', side_effect=start):
            result = self.call({'action': 'spawn', 'target': 'mail', 'message': 'hello',
                                'mode': 'resume', 'session_id': 'target-session'}, parent='parent')
            self.assertEqual(result['status'], 'starting')
            self.assertTrue(entered.wait(1))
            task_id = result['task_id']
            task = tasks._TASKS[task_id]
            self.assertEqual(task['owner'], (str(self.root / 'profiles/coding'), 'parent'))
            self.assertEqual(task['requested_session_id'], 'target-session')
            self.assertEqual(self.call({'action': 'status', 'task_id': task_id}, parent='other')['status'], 'failed')

    def test_acl_denial_prevents_spawn(self):
        with patch.object(tasks, 'check', return_value={'message': 'denied'}), \
                patch.object(tasks, '_start') as start:
            result = self.call({'action': 'spawn', 'target': 'local', 'message': 'hello'})
        self.assertEqual(result['status'], 'blocked')
        start.assert_not_called()

    def test_acl_denial_prevents_resume_spawn(self):
        with patch.object(tasks, 'check', return_value={'message': 'denied'}), \
                patch.object(tasks, '_start') as start:
            result = self.call({'action': 'spawn', 'target': 'local', 'message': 'hello',
                                'mode': 'resume', 'session_id': 'valid-looking-session'})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('denied', result['error'])
        start.assert_not_called()


class ProcessCleanupTests(unittest.TestCase):
    def make_child(self):
        proc = Mock()
        proc.stdout.readline.return_value = ''
        proc.stdout.close = Mock()
        proc.stdin.close = Mock()
        proc.poll.return_value = None
        with patch.object(tasks.subprocess, 'Popen', return_value=proc):
            child = tasks.Child([sys.executable, '-c', 'pass'], {}, '/')
        return child

    def test_no_wallclock_timer_and_stop_still_reaps(self):
        child = self.make_child()
        self.assertFalse(hasattr(child, 'timer'))
        self.assertFalse(hasattr(child, 'deadline'))
        with patch.object(tasks.ask_bot, '_stop_group') as stop:
            child.close()
        stop.assert_called_once_with(child.proc)
        self.assertTrue(child.closed)

    def test_session_error_event_settles_failure_without_a_timer(self):
        proc = Mock()
        proc.stdout.readline.side_effect = [
            json.dumps({'jsonrpc': '2.0', 'method': 'event',
                        'params': {'type': 'error', 'session_id': 'runtime',
                                   'payload': {'message': 'ownership refused'}}}) + '\n',
            '',
        ]
        proc.stdout.close = Mock()
        proc.stdin.close = Mock()
        with patch.object(tasks.subprocess, 'Popen', return_value=proc), \
                patch.object(tasks.threading, 'Thread') as thread:
            child = tasks.Child([sys.executable, '-c', 'pass'], {}, '/')
        thread.assert_called_once()
        child.sid = 'runtime'
        child.read()
        self.assertTrue(child.done.is_set())
        self.assertEqual(child.outcome, 'failed')
        self.assertEqual(child.error, 'ownership refused')
        with patch.object(tasks.ask_bot, '_stop_group'):
            child.close()


if __name__ == '__main__':
    unittest.main()
