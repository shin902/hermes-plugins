import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch
spec = importlib.util.spec_from_file_location('admin_guard', Path(__file__).with_name('__init__.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BLOCKED = [[], ['chat'], ['chat', '-q', ''], ['chat', '--query='],
 ['-z', 'x'], ['-zEMPTY'], ['-wzPROMPT'], ['--oneshot=x'], ['--ones=x'],
 ['--tui'], ['--cli'], ['--resume', 'latest'], ['-rlatest'], ['--continue'], ['-c'],
 ['desktop'], ['gui'], ['dashboard'], ['serve'], ['proxy'], ['acp'], ['rl'],
 ['mcp', 'serve'], ['gateway'], ['gateway', 'run'], ['gateway', 'start'],
 ['gateway', 'install'], ['gateway', 'restart'], ['gateway', 'migrate'],
 ['peer', 'dm', 'unused', 'x'], ['peer', 'run', 'unused', 'x'],
 ['cron', 'run', 'unused'], ['cron', 'tick'], ['curator', 'run'],
 ['--', 'chat'], ['--model', 'config'], ['--in', 'config'], ['--safe-mode'],
 ['serve', '--host', '--status'], ['serve', '--host=--stop'],
 ['--model', '--help', 'chat'], ['--run-module', 'run_agent'],
 ['--run-module', 'hermes_cli.main'], ['--run-module', 'arbitrary_plugin']]
ALLOWED = [['--version'], ['--help'], ['--tui', '--version'],
 ['-p', 'default', 'config', 'get', 'terminal.backend'],
 ['--profile=default', 'config', 'get', 'terminal.backend'],
 ['config', 'get', 'terminal.backend', '-p', 'default'], ['profile', 'list'],
 ['profile', 'delete', 'unused'], ['config', 'set', 'any.key', 'chat'],
 ['config', 'set', 'any.key', '--oneshot'], ['config', 'set', 'any.key', 'spaces ; $(id)'],
 ['-p', 'default', 'config', 'set', 'terminal.backend', 'PLACEHOLDER_ONLY'],
 ['custom-plugin', '--arbitrary', 'chat', '--oneshot', 'https://x/chat'],
 ['future-admin', '--future=value', '--', 'chat'], ['--future-global', 'chat', 'future-admin'],
 ['-mchat', 'config', 'get', 'x'], ['--model', 'chat', 'config', 'get', 'x'],
 ['--tui', 'config', 'get', 'x'], ['console'], ['send', '--to', 'unused', 'chat'],
 ['dashboard', '--status'], ['dashboard', '--stop'], ['dashboard', 'register'],
 ['serve', '--status'], ['serve', '--stop'], ['serve', '--help'],
 ['chat', '--help'], ['desktop', '--help'], ['gateway', '--help'],
 ['gateway', 'start', '--help'], ['gateway', 'status'], ['gateway', 'stop'],
 ['gateway', 'migrate', '--dry-run'], ['gateway', 'enroll'], ['gateway', 'future-admin'],
 ['mcp'], ['mcp', 'list'], ['mcp', 'serve', '--help'],
 ['mcp', 'add', 'unused', '--args', '--profile=other', 'chat'],
 ['peer', 'list'], ['--reasoning', 'high', 'config', 'get', 'terminal.backend'],
 ['--print-runtime-command']]
class GuardTests(unittest.TestCase):
 def caller(self):
  return patch.dict(sys.modules, {'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: str(m.HOME))})
 def test_blocked_before_subprocess(self):
  for argv in BLOCKED:
   for prefix in (([],) if argv[:1] == ['--run-module'] else ([], ['-p', 'default'], ['--profile=other'])):
    with self.subTest(argv=prefix+argv), self.caller(), patch.object(m.subprocess, 'run') as run:
     result = json.loads(m.administer({'argv': prefix+argv}))
     self.assertFalse(result['ok'], result)
     run.assert_not_called()
 def test_passthrough_and_boundary(self):
  for argv in ALLOWED:
   with self.subTest(argv=argv), self.caller(), patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'output', '')) as run:
    self.assertTrue(json.loads(m.administer({'argv': argv}))['ok'])
    self.assertEqual(run.call_args.args[0], [m.HERMES, *argv])
    self.assertIs(run.call_args.kwargs['shell'], False)
    self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
    self.assertEqual(run.call_args.kwargs['timeout'], 600)
 def test_invalid(self):
  for args in (None, {}, {'argv': 'chat'}, {'argv': [1]}, {'argv': ['\x00']}, {'argv': [], 'extra': 1}):
   with self.caller(), patch.object(m.subprocess, 'run') as run:
    self.assertFalse(json.loads(m.administer(args))['ok'])
    run.assert_not_called()
 def test_schema(self):
  ctx = Mock()
  m.register(ctx)
  schema = ctx.register_tool.call_args.kwargs['schema']
  self.assertEqual(schema['name'], 'hermes_cli')
  self.assertEqual(schema['parameters']['required'], ['argv'])
  self.assertEqual(set(schema['parameters']['properties']), {'argv'})
  self.assertIn('cross-profile', schema['description'])
  print('FRESH_REGISTRATION_SCHEMA', json.dumps(schema))
 def test_output_contract(self):
  with self.caller(), patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7, 'out', 'err')):
   r=json.loads(m.administer({'argv':['future-admin']}))
   self.assertEqual((r['ok'],r['exit_code'],r['stdout'],r['stderr'],r['timed_out']), (True,7,'out','err',False))
  with self.caller(), patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixed',600,output=b'partial',stderr=b'error')):
   r=json.loads(m.administer({'argv':['future-admin']}))
   self.assertTrue(r['timed_out']); self.assertIsNone(r['exit_code'])
   self.assertEqual((r['stdout'],r['stderr']),('partial','error'))
 def test_wrong_caller(self):
  with patch.dict(sys.modules, {'hermes_constants':types.SimpleNamespace(get_hermes_home=lambda:'/different')}), patch.object(m.subprocess,'run') as run:
   self.assertFalse(json.loads(m.administer({'argv':['--version']}))['ok'])
   run.assert_not_called()
 def test_environment(self):
  with patch.dict('os.environ', {'API_KEY': 'private', 'PYTHONPATH': '/bad'}):
   env = m.environment()
  self.assertNotIn('API_KEY', env)
  self.assertNotIn('PYTHONPATH', env)
if __name__ == '__main__':
 unittest.main(verbosity=2)
