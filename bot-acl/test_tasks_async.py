"""Focused async admission and official stdio smoke checks; no model calls."""
import json
import threading
import time
import unittest
from unittest.mock import patch, Mock
from test_tasks import TaskTests, tasks


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
                result = self.call('spawn', target='coding', message='hello')
                self.assertEqual(result['status'], 'starting')
                self.assertLess(time.monotonic()-before, .5)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()

    def test_cancel_before_start(self):
        tasks._TASKS['t']['child'] = None
        self.assertEqual(self.call('stop', task_id='t')['status'], 'stopped_unknown')
        with patch.object(tasks, 'Child') as ctor:
            tasks._start(tasks._TASKS['t'], 'coding', 'do not run', 'new_session', None)
            ctor.assert_not_called()

    def test_wait_registered_and_handled(self):
        self.assertIn('wait', tasks.SCHEMA['parameters']['properties']['action']['enum'])
        self.assertIn('status/wait/stop/steer require task_id', tasks.SCHEMA['description'])
        self.assertNotIn('list and wait take no extra fields', tasks.SCHEMA['description'])
        self.child.done.set()
        self.child.rpc.return_value = {'running':False}
        self.assertEqual(self.call('wait', task_id='t')['status'], 'completed')

    def test_schema_handler_isolation(self):
        import jsonschema
        ctx = Mock()
        tasks.register(ctx, {})
        callback = ctx.register_tool.call_args.args[3]
        payload = {'action':'list'}
        jsonschema.validate(payload, tasks.SCHEMA['parameters'])
        self.assertIn('tasks', json.loads(callback(payload, session_id='parent')))
        jsonschema.validate({'action':'spawn','message':'hello','target':'coding','mode':'resume',
                             'session_id':'target-session'}, tasks.SCHEMA['parameters'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({'action':'spawn','unknown':'forbidden'}, tasks.SCHEMA['parameters'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({'action':'spawn','message':'hello','target':'coding',
                                 'timeout_seconds':0}, tasks.SCHEMA['parameters'])
        rejected = self.call('spawn', target='coding', message='hello', timeout_seconds=0)
        self.assertEqual(rejected['status'], 'failed')
        self.assertIn('timeout_seconds is not supported', rejected['error'])
        rejected = self.call('list', timeout_seconds=0)
        self.assertEqual(rejected['status'], 'failed')
        self.assertIn('timeout_seconds is not supported', rejected['error'])


class OfficialTransportSmoke(unittest.TestCase):
    def test_official_subprocess_ready_and_capabilities(self):
        import importlib.util
        import os
        import sys
        from pathlib import Path
        spec = importlib.util.find_spec('tui_gateway')
        source = str(Path(spec.origin).parent.parent)
        # Isolate bootstrap from all real profiles and never submit an AI prompt.
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HERMES_HOME=home, PYTHONPATH=source)
            env.pop('HERMES_TUI_ATTACH_URL', None)
            child = tasks.Child([sys.executable,'-m','tui_gateway.entry'], env, source)
            try:
                self.assertTrue(child.ready.wait(20))
                self.assertIsNone(child.proc.poll())
                result = child.rpc('client.capabilities', {'server_requests':True})
                self.assertIsInstance(result, dict)
            finally:
                child.close()
