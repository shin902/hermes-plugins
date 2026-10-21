"""Focused asynchronous admission and schema checks; no model calls."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from test_tasks import TaskTests, tasks
from _task_fixtures import DummyTurn


class AsyncTests(TaskTests):
    def test_spawn_returns_before_startup(self):
        entered = threading.Event()
        release = threading.Event()

        def start(*args):
            entered.set()
            release.wait(2)

        with patch.object(tasks, '_start', side_effect=start):
            try:
                before = time.monotonic()
                result = self.call('spawn', target='mail', message='hello')
                self.assertEqual(result['status'], 'starting')
                self.assertLess(time.monotonic() - before, .5)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()

    def test_cancel_before_start_does_not_launch(self):
        tasks._TASKS['t']['turn'] = None
        self.assertEqual(self.call('stop', task_id='t')['status'], 'stopped')
        with patch.object(tasks, '_launch') as launch:
            tasks._start(tasks._TASKS['t'], 'mail', 'do not run', 'new_session', None)
            launch.assert_called_once()
        # _launch itself refuses a cancelled task without creating a turn.
        self.assertIsNone(tasks._TASKS['t']['turn'])

    def test_wait_registered_and_handled(self):
        self.assertIn('wait', tasks.SCHEMA['parameters']['properties']['action']['enum'])
        self.assertIn('status/wait/stop/steer require task_id', tasks.SCHEMA['description'])
        self.turn.done.set()
        self.turn.outcome = 'completed'
        self.assertEqual(self.call('wait', task_id='t')['status'], 'completed')

    def test_schema_handler_isolation(self):
        import jsonschema
        ctx = __import__('unittest.mock', fromlist=['Mock']).Mock()
        tasks.register(ctx, {})
        callback = ctx.register_tool.call_args.args[3]
        payload = {'action': 'list'}
        jsonschema.validate(payload, tasks.SCHEMA['parameters'])
        self.assertIn('tasks', json.loads(callback(payload, session_id='parent')))
        jsonschema.validate({'action': 'spawn', 'message': 'hello', 'target': 'mail',
                             'mode': 'resume', 'session_id': 'target-session'},
                            tasks.SCHEMA['parameters'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({'action': 'spawn', 'unknown': 'forbidden'},
                                tasks.SCHEMA['parameters'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({'action': 'spawn', 'message': 'hello', 'target': 'mail',
                                 'timeout_seconds': 0}, tasks.SCHEMA['parameters'])
        rejected = self.call('spawn', target='mail', message='hello', timeout_seconds=0)
        self.assertEqual(rejected['status'], 'failed')
        self.assertIn('timeout_seconds is not supported', rejected['error'])
        rejected = self.call('list', timeout_seconds=0)
        self.assertEqual(rejected['status'], 'failed')
        self.assertIn('timeout_seconds is not supported', rejected['error'])

    def _add_task(self, key, status, turn):
        tasks._TASKS[key] = {
            'id': key, 'owner': (str(self.source), 'parent'),
            'target': 'mail', 'mode': 'new_session', 'requested_session_id': None,
            'status': status, 'turn': turn, 'lock': threading.RLock(),
            'stop': threading.Event(), 'cancelled': False, 'turns': 1,
            'steer_count': 0, 'steer_status': None, 'session_id': 'runtime'}

    def _blocking_handoff(self, released):
        entered = threading.Event()
        replacement = DummyTurn(session_id='runtime')

        def launch(*args, **kwargs):
            entered.set()
            released.wait(5)
            return replacement

        return entered, launch

    def test_interrupting_handoff_counts_toward_owner_limit(self):
        for index in range(3):
            self._add_task('extra' + str(index), 'running', DummyTurn())
        released = threading.Event()
        entered, launch = self._blocking_handoff(released)
        steered = []
        with patch.object(tasks, '_launch', side_effect=launch), \
                patch.object(tasks, '_start'):
            thread = threading.Thread(target=lambda: steered.append(
                self.call('steer', task_id='t', message='handoff')))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.turn.done.is_set())
                self.assertEqual(tasks._TASKS['t']['status'], 'interrupting')
                result = self.call('spawn', target='mail', message='too many')
                self.assertEqual(result['status'], 'failed')
                self.assertIn('active child task limit', result['error'])
            finally:
                released.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(steered[0]['steer_status'], 'resumed')

    def test_interrupting_handoff_survives_completed_history_eviction(self):
        for index in range(3):
            self._add_task('done' + str(index), 'completed',
                           DummyTurn(done=True, outcome='completed'))
        released = threading.Event()
        entered, launch = self._blocking_handoff(released)
        with patch.object(tasks, 'RETAIN_COMPLETED', 1), \
                patch.object(tasks, '_launch', side_effect=launch), \
                patch.object(tasks, '_start'):
            thread = threading.Thread(target=lambda: self.call(
                'steer', task_id='t', message='handoff'))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                result = self.call('spawn', target='mail', message='pressure')
                self.assertEqual(result['status'], 'starting')
                self.assertIn('t', tasks._TASKS)
                self.assertEqual(tasks._TASKS['t']['status'], 'interrupting')
            finally:
                released.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_cancelled_interrupting_handoff_is_not_active(self):
        tasks._TASKS['t']['status'] = 'interrupting'
        self.turn.done.set()
        self.assertTrue(tasks._is_active(tasks._TASKS['t']))
        tasks._TASKS['t']['cancelled'] = True
        self.assertFalse(tasks._is_active(tasks._TASKS['t']))

    def test_active_task_limits(self):
        for index in range(3):
            tasks._TASKS['extra' + str(index)] = {
                'id': 'extra' + str(index), 'owner': (str(self.source), 'parent'),
                'target': 'mail', 'mode': 'new_session', 'requested_session_id': None,
                'status': 'running', 'turn': DummyTurn(), 'lock': threading.RLock(),
                'stop': threading.Event(), 'cancelled': False, 'turns': 1,
                'steer_count': 0, 'steer_status': None, 'session_id': 'runtime'}
        with patch.object(tasks, '_start'):
            result = self.call('spawn', target='mail', message='hello')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('active child task limit', result['error'])


if __name__ == '__main__':
    unittest.main()
