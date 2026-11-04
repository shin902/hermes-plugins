import importlib.util
import json
import os
import signal
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('host_exec_plugin', Path(__file__).resolve().parents[1] / 'host_exec.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class HostExecTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='host-exec-home-')
        self.helper = types.ModuleType('hermes_constants')
        self.helper.get_hermes_home = lambda: self.home
        self.modules = mock.patch.dict(sys.modules, {'hermes_constants': self.helper})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def run_command(self, **args):
        return json.loads(plugin.handle(args))

    def test_command_cwd_environment_and_exit(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'HOST_EXEC_TEST': 'inherited'}):
            result = self.run_command(command='pwd; printf "$HOST_EXEC_TEST"; printf err >&2; exit 7', cwd=directory)
        self.assertEqual(result['stdout'], directory + '\ninherited')
        self.assertEqual(result['stderr'], 'err')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['timed_out'])
        self.assertFalse(result['truncated'])

    def test_default_cwd_and_timeout(self):
        with mock.patch.object(plugin.subprocess, 'Popen', wraps=plugin.subprocess.Popen) as popen:
            result = self.run_command(command='pwd')
        self.assertEqual(result['stdout'].strip(), self.home)
        self.assertEqual(popen.call_args.kwargs['cwd'], self.home)
        self.assertEqual(plugin.SCHEMA['parameters']['properties']['timeout']['default'], 180)

    def test_output_bounded_and_drained(self):
        result = self.run_command(command="python3 -c 'import sys;sys.stdout.write(\"x\"*200000);sys.stderr.write(\"y\"*200000)'", cwd='/tmp', timeout=5)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(result['stdout']), plugin.OUTPUT_LIMIT)
        self.assertEqual(len(result['stderr']), plugin.OUTPUT_LIMIT)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['timed_out'])

    def test_timeout_and_pipe_holding_descendant(self):
        for command, parent_exit in [('sleep 30', False), ('sleep 30 & exit 0', True)]:
            with self.subTest(command=command):
                start = time.monotonic()
                result = self.run_command(command=command, cwd='/tmp', timeout=1)
                self.assertLess(time.monotonic() - start, 4)
                self.assertTrue(result['timed_out'])
                self.assertFalse(result['retry_safe'])
                self.assertEqual(result['exit_code'], 0 if parent_exit else -signal.SIGKILL)

    def test_timeout_kills_pipe_holding_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'pid'
            result = self.run_command(command=f'sleep 30 & echo $! > {pid_file}; exit 0', cwd=directory, timeout=1)
            self.assertTrue(result['timed_out'])
            pid = int(pid_file.read_text())
            # A killed orphan may remain a zombie until its new parent reaps it.
            for _ in range(50):
                status = Path(f'/proc/{pid}/stat')
                if not status.exists() or status.read_text().split()[2] == 'Z':
                    break
                time.sleep(0.02)
            else:
                self.fail('descendant is still running')

    def test_invalid_inputs(self):
        for args in [{}, {'command': ''}, {'command': 'x\0'}, {'command': 1},
                     {'command': 'true', 'cwd': 'relative'}, {'command': 'true', 'cwd': None},
                     {'command': 'true', 'timeout': True}, {'command': 'true', 'timeout': 0},
                     {'command': 'true', 'timeout': 601}, {'command': 'true', 'extra': 1}]:
            with self.subTest(args=args):
                self.assertIn('error', json.loads(plugin.handle(args)))

    def test_any_profile_home_is_accepted(self):
        for home in (tempfile.mkdtemp(prefix='other-'), tempfile.mkdtemp(prefix='another-')):
            self.helper.get_hermes_home = lambda home=home: home
            result = self.run_command(command='pwd')
            self.assertNotIn('error', result)
            self.assertEqual(result['stdout'].strip(), home)

    def test_registration_and_approval(self):
        ctx = mock.Mock()
        plugin.register(ctx)
        ctx.register_hook.assert_called_once_with('pre_tool_call', plugin._pre_tool_call)
        self.assertEqual(ctx.register_tool.call_args.kwargs['name'], 'host_exec')
        self.assertIs(ctx.register_tool.call_args.kwargs['handler'], plugin.handle)
        a = plugin._pre_tool_call('host_exec', {'command': 'pwd'})
        b = plugin._pre_tool_call('host_exec', {'command': 'pwd'})
        self.assertEqual(a['action'], 'approve')
        self.assertIn('pwd', a['message'])
        self.assertNotEqual(a['rule_key'], b['rule_key'])
        self.assertIsNone(plugin._pre_tool_call('terminal', {}))


if __name__ == '__main__':
    unittest.main()
