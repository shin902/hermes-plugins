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
