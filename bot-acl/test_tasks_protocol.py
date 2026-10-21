"""Deterministic public-CLI subprocess integration and public CLI surface checks.

No model is invoked. The fake executable is a local Python script; the public
CLI checks only run ``--help`` and an empty ``sessions list`` against a
temporary HERMES_HOME.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_acl import acl
from _task_fixtures import make_fake_cli, read_events

tasks = sys.modules[acl.__name__ + '.bot_task']


class _ObservedLock:
    """RLock wrapper that records the interrupter owning the completion lock."""

    def __init__(self):
        self._lock = threading.RLock()
        self.interrupter_entered = threading.Event()

    def __enter__(self):
        self._lock.acquire()
        if threading.current_thread().name == 'bot-task-interrupt':
            self.interrupter_entered.set()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self):
        self._lock.release()


class TurnIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = make_fake_cli(self.root)
        self.log = self.root / 'events.log'
        self.env = dict(os.environ, FAKE_CLI_LOG=str(self.log),
                        HERMES_HOME=str(self.root))
        self.env.pop('FAKE_CLI_MODE', None)
        self.env.pop('FAKE_CLI_NO_SESSION_ID', None)
        self.env.pop('FAKE_CLI_OWNERSHIP_REFUSAL', None)

    def turn(self, mode='complete', payload='literal $(false)', turn_mode='new_session',
             requested_session_id=None, **env):
        child_env = dict(self.env, FAKE_CLI_MODE=mode, **env)
        argv = [str(self.cli), '-p', 'mail', 'chat', '--query-file', '-',
                '--format', 'stream-json', '--run-budget', '0', '--max-turns', '250']
        if turn_mode == 'resume':
            argv.append('--resume=' + (requested_session_id or 'requested-session'))
        return tasks.Turn(argv, payload, child_env, Path.home(), self.root,
                          'mail', turn_mode, requested_session_id)

    def test_complete_turn_parses_init_and_result(self):
        turn = self.turn()
        self.assertTrue(turn.done.wait(8))
        self.assertEqual(turn.outcome, 'completed')
        self.assertEqual(turn.session_id, '20260101_000000_abcdef')
        self.assertIn('literal $(false)', turn.reply)
        self.assertFalse(turn.forced)

    def test_graceful_interrupt_preserves_partial_and_session(self):
        turn = self.turn('graceful')
        self.assertTrue(turn.started.wait(5))
        deadline = time.monotonic() + 5
        while turn.session_id is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(turn.interrupt(), 'graceful')
        self.assertEqual(turn.outcome, 'interrupted')
        self.assertEqual(turn.session_id, '20260101_000000_abcdef')
        self.assertEqual(turn.partial_reply, 'interrupted partial')
        self.assertFalse(turn.forced)

    def test_cancelled_startup_sends_no_prompt_and_reaps_child(self):
        real_popen = subprocess.Popen
        ready, entered, release = (self.root / 'ready', threading.Event(),
                                   threading.Event())

        def ready_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 5
            while not ready.exists() and proc.poll() is None:
                if time.monotonic() > deadline:
                    break
                time.sleep(.01)
            entered.set()
            release.wait(5)
            return proc

        with patch.object(tasks.subprocess, 'Popen', side_effect=ready_popen):
            turn = self.turn('startup', payload='SECRET-PAYLOAD',
                             FAKE_CLI_READY=str(ready))
            self.assertTrue(entered.wait(5))
            interrupter = threading.Thread(
                target=turn.interrupt, kwargs={'grace': 5, 'kill_grace': 5})
            interrupter.start()
            self.assertTrue(turn._cancel_requested.wait(5))
            release.set()
            interrupter.join(10)
        self.assertTrue(turn.done.wait(5))
        self.assertFalse(interrupter.is_alive())
        self.assertEqual(turn.exit_code, 0)
        self.assertIsNotNone(turn.proc.poll(), 'child was not reaped')
        starts = [event for event in read_events(self.log) if event['event'] == 'start']
        self.assertEqual([event['prompt'] for event in starts], [''])
        self.assertNotIn('SECRET-PAYLOAD', self.log.read_text(encoding='utf-8'))
        self.assertFalse(self.alive(turn.proc.pid))

    def test_interrupt_after_leader_exit_does_not_hold_completion_lock(self):
        parsing = threading.Event()
        release = threading.Event()
        real_outcome = tasks.ask_bot._outcome

        def blocked_outcome(*args, **kwargs):
            parsing.set()
            release.wait(5)
            return real_outcome(*args, **kwargs)

        with patch.object(tasks.ask_bot, '_outcome', side_effect=blocked_outcome):
            turn = self.turn('complete')
            self.assertTrue(parsing.wait(8), 'turn did not reach result parsing')
            deadline = time.monotonic() + 5
            while turn.proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(turn.proc.poll(), 'leader did not exit')

            observed = _ObservedLock()
            turn.lock = observed
            results = []
            interrupter = threading.Thread(
                name='bot-task-interrupt', daemon=True,
                target=lambda: results.append(
                    turn.interrupt(grace=1.0, kill_grace=5.0)))
            started = time.monotonic()
            interrupter.start()
            self.assertTrue(observed.interrupter_entered.wait(2),
                            'interrupt did not enter the completion lock')
            release.set()
            interrupter.join(2)
            elapsed = time.monotonic() - started

        self.assertFalse(interrupter.is_alive(),
                         'interrupt waited for publication while holding its lock')
        self.assertLess(elapsed, 2.0)
        self.assertEqual(results, [None])
        self.assertTrue(turn.done.wait(2))
        self.assertEqual(turn.outcome, 'completed')

    def test_post_popen_failure_closes_stdin_and_reaps_child(self):
        class FailingStdin:
            def __init__(self, stream):
                self._stream = stream

            def write(self, data):
                raise RuntimeError('post-popen transport failure')

            def close(self):
                self._stream.close()

            @property
            def closed(self):
                return self._stream.closed

        class FailingProc:
            def __init__(self, proc):
                self._proc = proc
                self.stdin = FailingStdin(proc.stdin)

            def __getattr__(self, name):
                return getattr(self._proc, name)

        real_popen = subprocess.Popen

        def failing_popen(*args, **kwargs):
            return FailingProc(real_popen(*args, **kwargs))

        with patch.object(tasks, 'SHUTDOWN_GRACE', .2), \
                patch.object(tasks, 'KILL_GRACE', 1.0), \
                patch.object(tasks.subprocess, 'Popen', side_effect=failing_popen):
            turn = self.turn('stdinhold', payload='must not be written')
            self.assertTrue(turn.done.wait(8))

        self.assertEqual(turn.outcome, 'failed')
        self.assertIn('post-popen transport failure', turn.error)
        self.assertTrue(turn.started.is_set())
        self.assertIsNotNone(turn.proc.poll(), 'child was not reaped')
        self.assertTrue(turn.proc.stdin.closed, 'stdin was not closed')
        self.assertFalse(self.alive(turn.proc.pid))

    def test_payload_encoding_failure_never_starts_child(self):
        turn = self.turn('complete', payload='\ud800')
        self.assertTrue(turn.done.wait(5))
        self.assertEqual(turn.outcome, 'failed')
        self.assertIn('transport_failed', turn.error)
        self.assertIsNone(turn.proc)
        self.assertEqual(read_events(self.log), [])

    def test_forced_kill_marks_persistence_unknown(self):
        with patch.object(tasks, 'SHUTDOWN_GRACE', .2), patch.object(tasks, 'KILL_GRACE', 1.0):
            turn = self.turn('ignore')
            self.assertTrue(turn.started.wait(5))
            deadline = time.monotonic() + 5
            while turn.session_id is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(turn.interrupt(), 'forced')
        self.assertEqual(turn.outcome, 'forced_stop_unknown')
        self.assertTrue(turn.forced)
        self.assertIn('SIGKILL', turn.error)

    def test_terminal_session_id_wins_over_init(self):
        turn = self.turn('complete', FAKE_CLI_SESSION_ID='init-session',
                         FAKE_CLI_TERMINAL_SESSION_ID='terminal-session')
        self.assertTrue(turn.done.wait(8))
        self.assertEqual(turn.outcome, 'completed')
        self.assertEqual(turn.session_id, 'terminal-session')

    def test_init_session_id_remains_fallback(self):
        turn = self.turn('complete', FAKE_CLI_SESSION_ID='init-session',
                         FAKE_CLI_TERMINAL_SESSION_ID='')
        self.assertTrue(turn.done.wait(8))
        self.assertEqual(turn.session_id, 'init-session')

    def test_resume_requested_id_is_final_fallback(self):
        turn = self.turn('complete', turn_mode='resume',
                         requested_session_id='requested-session',
                         FAKE_CLI_NO_SESSION_ID='1')
        self.assertTrue(turn.done.wait(8))
        self.assertEqual(turn.session_id, 'requested-session')

    def test_missing_session_id_is_not_invented(self):
        turn = self.turn('complete', FAKE_CLI_NO_SESSION_ID='1')
        self.assertTrue(turn.done.wait(8))
        self.assertIsNone(turn.session_id)

    def test_tool_child_is_terminated_with_the_group(self):
        turn = self.turn('tool')
        self.assertTrue(turn.started.wait(5))
        deadline = time.monotonic() + 5
        while not any(event['event'] == 'tool' for event in read_events(self.log)):
            if time.monotonic() > deadline:
                self.fail('tool child did not start')
            time.sleep(.01)
        tool = next(event for event in read_events(self.log) if event['event'] == 'tool')
        self.assertEqual(turn.interrupt(), 'graceful')
        self.assertTrue(turn.done.wait(5))
        deadline = time.monotonic() + 5
        while self.alive(tool['pid']):
            if time.monotonic() > deadline:
                self.fail('tool child survived the process-group SIGTERM')
            time.sleep(.01)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

class PublicCliSurfaceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('hermes'), 'public hermes CLI is installed')
    def test_chat_help_exposes_required_public_flags(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HERMES_HOME=home)
            result = subprocess.run([shutil.which('hermes'), 'chat', '--help'],
                                    capture_output=True, text=True, timeout=30,
                                    env=env, cwd=home)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ('--query-file', '--format', 'stream-json', '--run-budget',
                     '--max-turns', '--resume'):
            self.assertIn(flag, result.stdout)

    @unittest.skipUnless(shutil.which('hermes'), 'public hermes CLI is installed')
    def test_sessions_list_works_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HERMES_HOME=home)
            result = subprocess.run([shutil.which('hermes'), 'sessions', 'list', '--limit', '1'],
                                    capture_output=True, text=True, timeout=30,
                                    env=env, cwd=home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No sessions found', result.stdout)


class NoPrivateTransportTests(unittest.TestCase):
    FORBIDDEN = {'tui_gateway', 'hermes_cli.active_sessions', 'hermes_state_sessions'}

    def test_runtime_and_tests_have_no_forbidden_private_imports(self):
        for path in Path(__file__).parent.glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    for forbidden in self.FORBIDDEN:
                        self.assertFalse(module == forbidden or module.startswith(forbidden + '.'),
                                         f'{path.name} imports {module}')

    def test_runtime_has_no_tui_protocol_strings(self):
        for name in ('bot_task.py', 'ask_bot.py'):
            text = Path(__file__).with_name(name).read_text(encoding='utf-8')
            for marker in ('tui_gateway', 'session.resume', 'prompt.submit',
                           'message.complete', 'session.steer', 'session.activate'):
                self.assertNotIn(marker, text, f'{name} contains {marker}')


if __name__ == '__main__':
    unittest.main()
