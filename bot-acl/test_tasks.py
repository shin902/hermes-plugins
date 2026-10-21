"""Parent ownership, ACL, validation and registration for public-CLI bot tasks."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_acl import acl
from _task_fixtures import DummyTurn
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

tasks = sys.modules[acl.__name__ + '.bot_task']
consult = sys.modules[acl.__name__ + '.ask_bot']


class TaskTests(unittest.TestCase):
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
        self.token = set_hermes_home_override(self.source)
        self.addCleanup(reset_hermes_home_override, self.token)
        self.patchers = [
            patch.object(tasks.ask_bot, 'identity', return_value=('coding', self.root)),
            patch.object(tasks.ask_bot, 'get_hermes_home', return_value=str(self.source)),
            patch.object(tasks, 'check', return_value=None),
            patch.dict(tasks.os.environ, {tasks.ask_bot.DEPTH_ENV: '0'}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = tasks._TASKS.copy()
        tasks._TASKS.clear()
        self.addCleanup(lambda: (tasks._TASKS.clear(), tasks._TASKS.update(self.saved)))
        self.turn = DummyTurn()
        tasks._TASKS['t'] = {'id': 't', 'owner': (str(self.source), 'parent'),
                             'target': 'mail', 'mode': 'new_session',
                             'requested_session_id': None, 'status': 'running',
                             'turn': self.turn, 'lock': threading.RLock(),
                             'stop': threading.Event(), 'cancelled': False,
                             'turns': 1, 'steer_count': 0, 'steer_status': None,
                             'session_id': 'runtime'}
        ctx = Mock()
        acl.register(ctx)
        registrations = [c for c in ctx.register_tool.call_args_list if c.args[0] == 'bot_task']
        self.assertEqual(len(registrations), 1)
        self.callback = registrations[0].args[3]

    def call(self, action, **args):
        return json.loads(self.callback({'action': action, **args}, session_id='parent'))

    def test_no_arbitrary_session(self):
        result = self.call('status', task_id='t', session_id='victim')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.turn.interrupt_calls, 0)

    def test_other_parent_cannot_read(self):
        result = json.loads(tasks.handle({}, {'action': 'status', 'task_id': 't'},
                                         session_id='other'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.turn.interrupt_calls, 0)

    def test_missing_parent_rejected(self):
        self.assertEqual(json.loads(tasks.handle({}, {'action': 'list'}))['status'], 'failed')

    def test_steer_interrupts_then_resumes_same_session(self):
        replacement = DummyTurn(session_id='runtime')
        with patch.object(tasks, '_launch', return_value=replacement) as launch:
            result = self.call('steer', task_id='t', message='correction')
        self.assertEqual(result['steer_status'], 'resumed')
        self.assertEqual(result['session_id'], 'runtime')
        self.assertEqual(result['steer_count'], 1)
        self.assertEqual(self.turn.interrupt_calls, 1)
        launch.assert_called_once()
        args = launch.call_args.args
        self.assertEqual(args[1], 'mail')
        self.assertEqual(args[2], 'correction')
        self.assertEqual(args[3], 'resume')
        self.assertEqual(args[4], 'runtime')
        self.assertEqual(args[6], 'steer')

    def test_status_waits_for_settled_turn(self):
        self.turn.done.set()
        self.turn.outcome = 'completed'
        self.assertEqual(self.call('status', task_id='t')['status'], 'completed')

    def test_stop_interrupts_turn(self):
        result = self.call('stop', task_id='t')
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(self.turn.interrupt_calls, 1)
        self.assertTrue(result['interrupted'])

    def test_revoked_acl_hides_list_and_blocks_control(self):
        with patch.object(tasks, 'check', return_value={'message': 'revoked'}):
            self.assertEqual(self.call('list')['tasks'], [])
            self.assertEqual(self.call('stop', task_id='t')['status'], 'blocked')
            self.assertEqual(self.turn.interrupt_calls, 0)

    def test_nested_tasks_blocked(self):
        with patch.dict(tasks.os.environ, {tasks.ask_bot.DEPTH_ENV: '1'}):
            self.assertEqual(self.call('list')['status'], 'failed')

    def test_registration(self):
        ctx = Mock()
        tasks.register(ctx, {})
        self.assertEqual(ctx.register_tool.call_args.args[:3], ('bot_task', 'bot_consult', tasks.SCHEMA))

    def test_plugin_exposes_sync_and_async_tools(self):
        ctx = Mock()
        acl.register(ctx)
        names = [call.args[0] for call in ctx.register_tool.call_args_list]
        self.assertEqual(names, ['ask_bot', 'list_bot_peers', 'bot_task'])
        manifest = Path(__file__).with_name('plugin.yaml').read_text()
        self.assertIn('ask_bot', manifest)
        self.assertIn('bot_task', manifest)
        self.assertTrue(callable(consult.register))
        self.assertEqual(consult.ASK_SCHEMA['name'], 'ask_bot')

    def test_prompts_guide_both_tools_without_bot_chat(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(directory)
            ctx = PluginContext(PluginManifest(name='bot-acl'), manager)
            consult.register_prompt(ctx)
            tasks.register_prompt(ctx)
            sections = manager.render_system_prompt_sections({'session_id': 'ordinary', 'title': 'ordinary'})
        text = '\n'.join(section.content for section in sections)
        self.assertIn('ask_bot', text)
        self.assertIn('bot_task', text)
        self.assertIn('SIGTERM', text)
        self.assertNotIn('bot_chat', text)

    def test_timeout_seconds_is_explicitly_rejected(self):
        for action in ('spawn', 'list', 'status', 'wait', 'steer', 'stop'):
            args = {'action': action, 'timeout_seconds': 0}
            if action in ('spawn', 'steer'):
                args.update(target='mail', message='hello')
            if action != 'spawn':
                args['task_id'] = 't'
            result = self.call(**args)
            self.assertEqual(result['status'], 'failed', action)
            self.assertIn('timeout_seconds is not supported', result['error'])

    def test_schema_has_no_timeout_and_no_bot_chat(self):
        properties = tasks.SCHEMA['parameters']['properties']
        self.assertNotIn('timeout_seconds', properties)
        self.assertEqual(properties['mode']['enum'], ['new_session', 'resume'])
        self.assertIn('SIGTERM', tasks.SCHEMA['description'])
        self.assertIn('original prompt', tasks.SCHEMA['description'])


if __name__ == '__main__':
    unittest.main()
