"""bot_task public-CLI modes, steering, stop races and process cleanup; no model calls."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_acl import acl
from _task_fixtures import make_fake_cli, read_events, starts
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

tasks = sys.modules[acl.__name__ + '.bot_task']


class TaskCliTests(unittest.TestCase):
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
        self.source = self.root / 'profiles/coding'
        self.target = self.root / 'profiles/mail'
        self.token = set_hermes_home_override(self.source)
        self.addCleanup(reset_hermes_home_override, self.token)
        self.log = self.root / 'fake-cli.log'
        self.cli = make_fake_cli(self.root)
        self.patchers = [
            patch.object(tasks.ask_bot, 'identity', return_value=('coding', self.root)),
            patch.object(tasks.ask_bot, 'get_hermes_home', return_value=str(self.source)),
            patch.object(tasks, 'check', return_value=None),
            patch.object(tasks.ask_bot.shutil, 'which', return_value=str(self.cli)),
            patch.dict(tasks.os.environ, {tasks.ask_bot.DEPTH_ENV: '0',
                                          'FAKE_CLI_LOG': str(self.log),
                                          'FAKE_CLI_MODE': 'complete'}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = tasks._TASKS.copy()
        tasks._TASKS.clear()
        self.addCleanup(lambda: (tasks._TASKS.clear(), tasks._TASKS.update(self.saved)))
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for task in list(tasks._TASKS.values()):
            turn = task.get('turn')
            if turn is not None and not turn.done.is_set():
                turn.interrupt(grace=0.2, kill_grace=1.0)

    def fake(self, mode, **env):
        return patch.dict(os.environ, {'FAKE_CLI_MODE': mode, **env})

    def call(self, args, parent='parent'):
        return json.loads(tasks.handle({}, args, session_id=parent))

    def spawn(self, target='mail', message='initial prompt', mode='new_session',
              session_id=None):
        args = {'action': 'spawn', 'target': target, 'message': message, 'mode': mode}
        if session_id is not None:
            args['session_id'] = session_id
        return self.call(args)

    def wait_for(self, predicate, timeout=8, message='condition'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('timed out waiting for ' + message)

    def task(self, task_id):
        return tasks._TASKS[task_id]

    def wait_running(self, task_id, session=True):
        def ready():
            task = self.task(task_id)
            turn = task.get('turn')
            return (task['status'] == 'running' and turn is not None
                    and (not session or turn.session_id))
        self.wait_for(ready, message='task running')
        return self.task(task_id)

    def wait_status(self, task_id, status):
        self.wait_for(lambda: self.task(task_id)['status'] == status,
                      message='task status ' + status)
        return self.task(task_id)

    def seed_session(self, target='mail', session_id='target-session'):
        home = self.root if target == 'default' else self.root / 'profiles' / target
        with closing(sqlite3.connect(home / 'state.db')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)')
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?)', (session_id,))
            db.commit()

    def test_new_session_uses_public_cli_and_parses_result(self):
        result = self.spawn()
        task = self.wait_status(result['task_id'], 'completed')
        snapshot = tasks._snapshot(task)
        self.assertIn('initial prompt', snapshot['reply'])
        self.assertTrue(snapshot['finality_verified'])
        self.assertEqual(snapshot['transport'], 'public-cli-stream-json')
        events = starts(self.log)
        self.assertEqual(len(events), 1)
        event = events[0]
        argv = event['argv']
        self.assertEqual(argv[argv.index('-p') + 1], 'mail')
        self.assertIn('chat', argv)
        self.assertEqual(argv[argv.index('--query-file') + 1], '-')
        self.assertEqual(argv[argv.index('--format') + 1], 'stream-json')
        self.assertEqual(argv[argv.index('--source') + 1], 'cli')
        self.assertEqual(argv[argv.index('--run-budget') + 1], '0')
        self.assertEqual(argv[argv.index('--max-turns') + 1], '250')
        self.assertNotIn('--continue', argv)
        self.assertFalse(any(arg.startswith('--resume=') for arg in argv))
        self.assertEqual(event['home'], str(self.target))
        self.assertIn('initial prompt', event['prompt'])

    def test_resume_uses_exact_id_and_public_cli(self):
        self.seed_session('mail', 'target-session')
        result = self.spawn(mode='resume', session_id='target-session')
        task = self.wait_status(result['task_id'], 'completed')
        snapshot = tasks._snapshot(task)
        self.assertEqual(snapshot['session_id'], 'target-session')
        self.assertEqual(snapshot['requested_session_id'], 'target-session')
        events = starts(self.log)
        self.assertEqual(len(events), 1)
        argv = events[0]['argv']
        self.assertIn('--resume=target-session', argv)
        self.assertNotIn('--continue', argv)
        self.assertNotIn('--create-if-missing', argv)
        self.assertEqual(events[0]['target'], 'mail')
        self.assertEqual(events[0]['home'], str(self.target))

    def test_completed_turn_prefers_terminal_session_id_over_init(self):
        with self.fake('complete', FAKE_CLI_SESSION_ID='init-session',
                       FAKE_CLI_TERMINAL_SESSION_ID='terminal-session'):
            result = self.spawn()
            task = self.wait_status(result['task_id'], 'completed')
        snapshot = tasks._snapshot(task)
        self.assertEqual(snapshot['session_id'], 'terminal-session')

    def test_resume_rejects_missing_foreign_prefix_title_and_latest(self):
        self.seed_session('mail', 'target-session')
        self.seed_session('default', 'other-profile')
        cases = [
            ('mail', 'other-profile'),
            ('mail', 'missing-title'),
            ('mail', 'target'),
            ('mail', 'latest'),
            ('local', 'target-session'),
        ]
        for target, session_id in cases:
            result = self.spawn(target=target, mode='resume', session_id=session_id)
            if result.get('status') == 'failed':
                continue
            task = self.wait_status(result['task_id'], 'failed_unknown')
            self.assertIn('not found', tasks._snapshot(task)['error'])
        self.assertEqual(starts(self.log), [])

    def test_ownership_refusal_is_target_busy_without_resend(self):
        self.seed_session('mail', 'target-session')
        with self.fake('complete', FAKE_CLI_OWNERSHIP_REFUSAL='1'):
            result = self.spawn(mode='resume', session_id='target-session')
            task = self.wait_status(result['task_id'], 'target_busy')
        self.assertIn('owned by another live process', tasks._snapshot(task)['error'])
        self.assertEqual(len(starts(self.log)), 1)

    def test_partial_result_is_preserved_and_not_completed(self):
        with self.fake('partial'):
            result = self.spawn()
            task = self.wait_status(result['task_id'], 'failed')
        snapshot = tasks._snapshot(task)
        self.assertEqual(snapshot['partial_reply'], 'partial evidence')
        self.assertEqual(snapshot['error'], 'quota')
        self.assertEqual(snapshot['reply'], '')

    def test_steer_interrupts_generation_and_resumes_without_original_prompt(self):
        with self.fake('graceful'):
            result = self.spawn()
            task_id = result['task_id']
            task = self.wait_running(task_id)
            session_id = task['turn'].session_id
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'new guidance'})
            self.assertEqual(steered['status'], 'running')
            self.assertEqual(steered['steer_status'], 'resumed')
            self.assertEqual(steered['steer_count'], 1)
            self.assertEqual(steered['session_id'], session_id)
            self.wait_for(lambda: len(starts(self.log)) == 2, message='resumed turn')
            self.wait_running(task_id)
        events = starts(self.log)
        self.assertEqual(len(events), 2)
        first, second = events
        self.assertIn('initial prompt', first['prompt'])
        self.assertIn('new guidance', second['prompt'])
        self.assertNotIn('initial prompt', second['prompt'])
        self.assertIn('--resume=' + session_id, second['argv'])
        self.assertIn('--run-budget', second['argv'])
        self.assertEqual(second['argv'][second['argv'].index('--run-budget') + 1], '0')
        self.assertEqual(second['argv'][second['argv'].index('--max-turns') + 1], '250')
        self.assertIn('term', [event['event'] for event in read_events(self.log)])

    def test_steer_resumes_terminal_id_after_compression_continuation(self):
        with self.fake('graceful', FAKE_CLI_SESSION_ID='init-session',
                       FAKE_CLI_TERMINAL_SESSION_ID='terminal-session'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'continue after compression'})
            self.assertEqual(steered['steer_status'], 'resumed')
            self.assertEqual(steered['session_id'], 'terminal-session')
            self.wait_for(lambda: len(starts(self.log)) == 2, message='resumed turn')
        second = starts(self.log)[1]
        self.assertIn('--resume=terminal-session', second['argv'])
        self.assertNotIn('--resume=init-session', second['argv'])
        self.assertIn('continue after compression', second['prompt'])

    def test_steer_while_tool_running_terminates_tool_process(self):
        with self.fake('tool'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            self.wait_for(lambda: any(event['event'] == 'tool' for event in read_events(self.log)),
                          message='tool child')
            tool = next(event for event in read_events(self.log) if event['event'] == 'tool')
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'tool guidance'})
            self.assertEqual(steered['steer_status'], 'resumed')
            self.wait_for(lambda: len(starts(self.log)) == 2, message='resumed turn')
            self.wait_for(lambda: not self._alive(tool['pid']), message='tool child exit')
        self.assertTrue(self._alive(tasks._TASKS[task_id]['turn'].proc.pid)
                        or tasks._TASKS[task_id]['turn'].done.is_set())

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_completion_before_steer_sends_nothing(self):
        result = self.spawn()
        task_id = result['task_id']
        self.wait_status(task_id, 'completed')
        steered = self.call({'action': 'steer', 'task_id': task_id,
                             'message': 'too late'})
        self.assertEqual(steered['status'], 'completed')
        self.assertEqual(steered['steer_status'], 'not_applied_completed')
        self.assertEqual(len(starts(self.log)), 1)
        self.assertNotIn('too late', starts(self.log)[0]['prompt'])

    def test_failed_turn_before_steer_is_not_reported_as_completed(self):
        with self.fake('partial'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_status(task_id, 'failed')
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'too late'})
        self.assertEqual(steered['status'], 'failed')
        self.assertEqual(steered['steer_status'], 'not_applied')
        # The real failure is preserved, not rewritten as a steer/completion note.
        self.assertEqual(steered['error'], 'quota')
        self.assertEqual(len(starts(self.log)), 1)

    def test_missing_session_id_blocks_resume(self):
        with self.fake('graceful', FAKE_CLI_NO_SESSION_ID='1'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id, session=False)
            self.wait_for(lambda: len(starts(self.log)) == 1, message='first turn')
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'cannot resume'})
            self.assertEqual(steered['status'], 'steer_blocked')
            self.assertEqual(steered['steer_status'], 'blocked_no_session_id')
            self.assertIsNone(steered['session_id'])
            self.assertIn('no session_id', steered['error'])
            self.assertNotIn('CLI turn was interrupted', steered['error'])
            self.assertEqual(len(starts(self.log)), 1)
            self.assertNotIn('cannot resume', starts(self.log)[0]['prompt'])
            # A later refresh must not replace the actionable cause with the
            # predecessor turn's interruption error.
            status = self.call({'action': 'status', 'task_id': task_id})
        self.assertEqual(status['status'], 'steer_blocked')
        self.assertEqual(status['error'], steered['error'])

    def test_steer_acl_revocation_preserves_actionable_error(self):
        with self.fake('graceful'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            with patch.object(tasks, 'check',
                              side_effect=[None, {'message': 'revoked mid-task'}]):
                steered = self.call({'action': 'steer', 'task_id': task_id,
                                     'message': 'must not send'})
        self.assertEqual(steered['status'], 'blocked')
        self.assertEqual(steered['steer_status'], 'blocked_acl')
        self.assertEqual(steered['error'], 'revoked mid-task')
        self.assertEqual(len(starts(self.log)), 1)

    def test_steer_launch_failure_preserves_actionable_error(self):
        with self.fake('graceful'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            with patch.object(tasks.ask_bot, '_executable', return_value=None):
                steered = self.call({'action': 'steer', 'task_id': task_id,
                                     'message': 'replacement must fail'})
        self.assertEqual(steered['status'], 'steer_blocked')
        self.assertEqual(steered['steer_status'], 'blocked_launch_failed')
        self.assertIn('Hermes CLI is not available', steered['error'])
        self.assertNotIn('CLI turn was interrupted', steered['error'])
        self.assertEqual(len(starts(self.log)), 1)

    def test_stop_while_starting_marks_stopped(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        real_launch = tasks._launch

        def launch(*args, **kwargs):
            entered.set()
            try:
                release.wait(5)
                return real_launch(*args, **kwargs)
            finally:
                finished.set()

        with patch.object(tasks, '_launch', side_effect=launch), self.fake('complete'):
            result = self.spawn()
            task_id = result['task_id']
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.task(task_id)['status'], 'starting')
                self.assertIsNone(self.task(task_id)['turn'])
                stopped = self.call({'action': 'stop', 'task_id': task_id})
            finally:
                release.set()
            self.assertTrue(finished.wait(5))
        self.assertEqual(stopped['status'], 'stopped')
        self.assertEqual(starts(self.log), [])

    def test_stop_after_settled_startup_failure_preserves_status(self):
        result = self.spawn(mode='resume', session_id='missing-session')
        task = self.wait_status(result['task_id'], 'failed_unknown')
        error = tasks._snapshot(task)['error']
        stopped = self.call({'action': 'stop', 'task_id': result['task_id']})
        self.assertEqual(stopped['status'], 'failed_unknown')
        self.assertEqual(stopped['stop_status'], 'already_finished')
        self.assertEqual(stopped['error'], error)
        self.assertEqual(starts(self.log), [])

    def test_forced_shutdown_blocks_resume(self):
        with self.fake('ignore'), patch.object(tasks, 'SHUTDOWN_GRACE', .2), \
                patch.object(tasks, 'KILL_GRACE', 1.0):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'must not resume'})
        self.assertEqual(steered['status'], 'steer_blocked')
        self.assertEqual(steered['steer_status'], 'blocked_forced_kill')
        self.assertTrue(steered['forced_kill'])
        self.assertIn('SIGKILL', steered['error'])
        self.assertEqual(len(starts(self.log)), 1)
        self.assertNotIn('must not resume', starts(self.log)[0]['prompt'])

    def test_stop_wins_steer_race(self):
        with self.fake('slowterm', FAKE_CLI_TERM_DELAY='0.5'), \
                patch.object(tasks, 'SHUTDOWN_GRACE', 2.0):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            steered = []
            thread = threading.Thread(target=lambda: steered.append(
                self.call({'action': 'steer', 'task_id': task_id,
                           'message': 'steer must lose'})))
            thread.start()
            self.wait_for(lambda: any(event['event'] == 'term'
                                      for event in read_events(self.log)),
                          message='SIGTERM sent')
            stopped = self.call({'action': 'stop', 'task_id': task_id})
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(steered[0]['steer_status'], 'stopped')
        self.assertIn(stopped['status'], ('stopped', 'stopped_unknown'))
        self.assertEqual(len(starts(self.log)), 1)
        self.assertNotIn('steer must lose', starts(self.log)[0]['prompt'])

    def test_multiple_steer_serializes_without_duplicate_sends(self):
        with self.fake('graceful'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            first = self.call({'action': 'steer', 'task_id': task_id,
                               'message': 'guidance one'})
            self.assertEqual(first['steer_status'], 'resumed')
            self.wait_for(lambda: len(starts(self.log)) == 2, message='second turn')
            self.wait_running(task_id)
            second = self.call({'action': 'steer', 'task_id': task_id,
                                'message': 'guidance two'})
            self.assertEqual(second['steer_status'], 'resumed')
            self.assertEqual(second['steer_count'], 2)
            self.wait_for(lambda: len(starts(self.log)) == 3, message='third turn')
        events = starts(self.log)
        self.assertEqual(len(events), 3)
        prompts = [event['prompt'] for event in events]
        self.assertEqual(sum('initial prompt' in prompt for prompt in prompts), 1)
        self.assertEqual(sum('guidance one' in prompt for prompt in prompts), 1)
        self.assertEqual(sum('guidance two' in prompt for prompt in prompts), 1)
        timeline = [event for event in read_events(self.log)
                    if event['event'] in ('start', 'end')]
        active = set()
        for event in timeline:
            if event['event'] == 'start':
                self.assertNotIn(event['pid'], active)
                active.add(event['pid'])
            else:
                active.discard(event['pid'])

    def test_stop_after_completion_does_not_rewrite_outcome(self):
        result = self.spawn()
        task_id = result['task_id']
        self.wait_status(task_id, 'completed')
        stopped = self.call({'action': 'stop', 'task_id': task_id})
        self.assertEqual(stopped['status'], 'completed')
        self.assertEqual(stopped['stop_status'], 'already_finished')
        self.assertEqual(len(starts(self.log)), 1)

    def test_stop_before_steer_prevents_resume(self):
        with self.fake('graceful'):
            result = self.spawn()
            task_id = result['task_id']
            self.wait_running(task_id)
            self.call({'action': 'stop', 'task_id': task_id})
            steered = self.call({'action': 'steer', 'task_id': task_id,
                                 'message': 'no resume'})
        self.assertEqual(steered['steer_status'], 'stopped')
        self.assertEqual(len(starts(self.log)), 1)
        self.assertNotIn('no resume', starts(self.log)[0]['prompt'])

    def test_acl_denial_prevents_spawn_and_control(self):
        with patch.object(tasks, 'check', return_value={'message': 'denied'}):
            result = self.spawn(target='local')
            self.assertEqual(result['status'], 'blocked')
            result = self.call({'action': 'stop', 'task_id': 'missing'})
            self.assertEqual(result['status'], 'failed')
        self.assertEqual(starts(self.log), [])

    def test_unknown_task_and_foreign_parent(self):
        result = self.spawn()
        task_id = result['task_id']
        self.wait_status(task_id, 'completed')
        self.assertEqual(self.call({'action': 'status', 'task_id': task_id},
                                   parent='other')['status'], 'failed')
        self.assertEqual(self.call({'action': 'status', 'task_id': 'nope'})['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
