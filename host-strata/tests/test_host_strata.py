import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'host_strata_plugin', Path(__file__).resolve().parents[1] / 'host_strata.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

IDLE = 'ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n'


class HostStrataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='host-strata-')))
        self.home = self.tmp / 'profiles' / 'coding'
        self.home.mkdir(parents=True)
        self.repo = self.tmp / 'Strata'
        self.wt = self.tmp / 'wt'
        self.models = self.tmp / 'models'
        for path in (self.repo, self.wt, self.models):
            path.mkdir()
        self.engine = self.models / 'flash.json'
        self.engine.write_text('{"exe": ".venv/bin/strata"}', encoding='utf-8')
        self.calls = []
        self.states = {}
        self.worktrees = f'worktree {self.repo}\nHEAD abc\n\nworktree {self.wt}\nHEAD def\n\n'

        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: str(self.home)
        yaml = types.ModuleType('hermes_yaml')
        yaml.safe_load = json.loads
        modules = mock.patch.dict(sys.modules, {'hermes_constants': constants,
                                                'hermes_yaml': yaml})
        modules.start()
        self.addCleanup(modules.stop)
        patcher = mock.patch.object(plugin, 'run', side_effect=self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(plugin, 'health', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_config()

    def write_config(self, **overrides):
        config = {'repo': str(self.repo), 'python': '.venv/bin/python',
                  'build_dir': 'build-test', 'build_jobs': 8,
                  'proxy_service': 'llama-swap', 'port': 8095,
                  'engine_config_dirs': [str(self.models)],
                  'default_engine_config': str(self.engine),
                  'env': {'STRATA_TEST': '{checkout}/data'}, 'cmake_args': [],
                  'lib_dirs': []}
        config.update(overrides)
        path = self.tmp / 'plugin-config' / 'host-strata' / 'config.yaml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding='utf-8')

    def fake_run(self, argv, timeout=60):
        self.calls.append(list(argv))
        if argv[:3] == ['systemctl', '--user', 'show']:
            return subprocess.CompletedProcess(argv, 0, self.states.get(argv[3], IDLE), '')
        if argv[:2] == ['git', '-C']:
            return subprocess.CompletedProcess(argv, 0, self.worktrees, '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def call(self, **args):
        return json.loads(plugin.handle(args))

    def launched(self):
        return [c for c in self.calls if c[0] == 'systemd-run']

    def test_missing_config_fails_closed(self):
        (self.tmp / 'plugin-config' / 'host-strata' / 'config.yaml').unlink()
        result = self.call(action='status')
        self.assertIn('missing config', result['error'])
        self.assertIn('host-strata', result['error'])

    def test_required_config_keys(self):
        self.write_config(port=None)
        self.assertIn('config port', self.call(action='status')['error'])

    def test_unknown_argument_rejected(self):
        self.assertIn('unknown argument', self.call(action='status', bogus=1)['error'])

    def test_checkout_must_be_repo_or_worktree(self):
        outside = self.tmp / 'elsewhere'
        outside.mkdir()
        self.assertIn('neither', self.call(action='configure', checkout=str(outside))['error'])
        result = self.call(action='configure', checkout=str(self.wt), build_dir='bt')
        self.assertNotIn('error', result)
        run_argv = self.launched()[-1]
        self.assertIn(str(self.wt), run_argv)
        self.assertIn(str(self.wt / 'bt'), run_argv)

    def test_build_dir_traversal_rejected(self):
        self.assertIn('build_dir', self.call(action='configure', build_dir='../evil')['error'])
        self.assertIn('build_dir', self.call(action='configure', build_dir='..')['error'])

    def test_engine_config_outside_allowed_dirs_rejected(self):
        stray = self.tmp / 'stray.json'
        stray.write_text('{}', encoding='utf-8')
        result = self.call(action='serve_start', engine_config=str(stray))
        self.assertIn('outside the allowed config dirs', result['error'])
        self.assertIn('absolute', self.call(action='serve_start',
                                           engine_config='stray.json')['error'])

    def test_serve_start_requires_proxy_stopped(self):
        self.states['llama-swap'] = 'ActiveState=active\nSubState=running\nResult=success\n'
        result = self.call(action='serve_start')
        self.assertIn('proxy_stop', result['error'])
        self.assertEqual(self.launched(), [])

    def test_serve_start_argv(self):
        result = self.call(action='serve_start')
        self.assertNotIn('error', result)
        self.assertEqual(result['url'], 'http://127.0.0.1:8095')
        argv = self.launched()[-1]
        self.assertIn('--unit', argv)
        self.assertEqual(argv[argv.index('--unit') + 1], 'host-strata-serve')
        self.assertIn(f'WorkingDirectory={self.repo}', argv)
        self.assertIn('--setenv', argv)
        self.assertIn(f'STRATA_TEST={self.repo}/data', argv)
        tail = argv[argv.index('--') + 1:]
        self.assertEqual(tail, [str(self.repo / '.venv/bin/python'), 'serve/server.py',
                                '--engine', 'strata', '--config', str(self.engine),
                                '--port', '8095'])

    def test_serve_start_records_provenance(self):
        exe = self.tmp / 'strata'
        exe.write_bytes(b'engine')
        self.engine.write_text(json.dumps({'exe': str(exe)}), encoding='utf-8')
        self.call(action='serve_start')
        record = json.loads((self.tmp / 'plugin-state' / 'host-strata'
                             / 'host-strata-serve.json').read_text(encoding='utf-8'))
        prov = record['provenance']
        self.assertEqual(prov['exe_sha256'], hashlib.sha256(b'engine').hexdigest())
        self.assertEqual(prov['engine_config'], str(self.engine))
        self.assertIn(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], self.calls)

    def test_python_cli_runs_venv_python_in_waiting_unit(self):
        self.write_config(lib_dirs=['/opt/lib'])
        done = subprocess.CompletedProcess([], 3, 'x' * 70000 + 'tail', None)
        with mock.patch.object(plugin.subprocess, 'run', return_value=done) as runner:
            result = json.loads(plugin.handle_python(
                {'args': ['-m', 'pip', 'install', '-r', 'requirements.txt'], 'timeout': 120}))
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:6], ['systemd-run', '--user', '--wait', '--pipe', '--collect',
                                    '--quiet'])
        self.assertIn('RuntimeMaxSec=120', argv)
        self.assertIn('LD_LIBRARY_PATH=/opt/lib', argv)
        self.assertEqual(argv[argv.index('--') + 1:],
                         [str(self.repo / '.venv/bin/python'), '-m', 'pip', 'install',
                          '-r', 'requirements.txt'])
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(result['timed_out'])
        self.assertTrue(result['truncated'])
        self.assertTrue(result['output'].endswith('tail'))

    def test_python_cli_validates_arguments(self):
        for bad in ({'args': []}, {'args': 'pip'}, {'args': ['-c', 'x'], 'timeout': 0},
                    {'args': ['-c', 'x'], 'cwd': '/'}):
            self.assertIn('error', json.loads(plugin.handle_python(bad)))

    def test_build_argv_with_target(self):
        build = self.repo / 'build-test'
        build.mkdir()
        (build / 'CMakeCache.txt').write_text('', encoding='utf-8')
        result = self.call(action='build', target='strata_cli')
        self.assertNotIn('error', result)
        tail = self.launched()[-1]
        tail = tail[tail.index('--') + 1:]
        self.assertEqual(tail, ['cmake', '--build', str(build), '-j', '8',
                                '--target', 'strata_cli'])

    def test_build_requires_configured_dir(self):
        self.assertIn('not configured', self.call(action='build')['error'])

    def test_ctest_refused_while_serve_runs(self):
        self.states['host-strata-serve'] = 'ActiveState=active\nSubState=running\n'
        result = self.call(action='test', suite='ctest')
        self.assertIn('serve', result['error'])
        self.assertEqual(self.launched(), [])

    def test_serve_suite_argv_and_library_path(self):
        self.write_config(lib_dirs=['/opt/libA', '/opt/libB'])
        with mock.patch.dict(os.environ, {'LD_LIBRARY_PATH': '/inherited'}):
            result = self.call(action='test', suite='serve', filter='test_api.py')
        self.assertNotIn('error', result)
        argv = self.launched()[-1]
        self.assertIn('LD_LIBRARY_PATH=/opt/libA:/opt/libB', argv)  # Hermes's env is not leaked
        tail = argv[argv.index('--') + 1:]
        self.assertEqual(tail, [str(self.repo / '.venv/bin/python'), '-m', 'unittest',
                                'discover', '-s', 'serve', '-t', '.', '-p',
                                'test_api.py'])

    def test_log_tail_and_job_exit_code(self):
        log = self.tmp / 'job.log'
        log.write_text('\n'.join(f'line{i}' for i in range(1, 11)) + '\n', encoding='utf-8')
        state_dir = self.tmp / 'plugin-state' / 'host-strata'
        state_dir.mkdir(parents=True)
        (state_dir / 'host-strata-job.json').write_text(
            json.dumps({'unit': 'host-strata-job', 'log': str(log)}), encoding='utf-8')
        self.states['host-strata-job'] = ('ActiveState=inactive\nSubState=exited\n'
                                          'Result=success\nExecMainStatus=7\n')
        result = self.call(action='log', lines=3)
        self.assertEqual(result['lines'], ['line8', 'line9', 'line10'])
        self.assertTrue(result['state']['finished'])
        self.assertEqual(result['state']['exit_code'], 7)

    def test_register(self):
        ctx = mock.Mock()
        plugin.register(ctx)
        tools = {c.kwargs['name']: c.kwargs for c in ctx.register_tool.call_args_list}
        self.assertEqual(set(tools), {'host_strata', 'python_cli'})
        self.assertEqual({t['toolset'] for t in tools.values()}, {'host_strata'})
        self.assertIs(tools['host_strata']['handler'], plugin.handle)
        self.assertIs(tools['python_cli']['handler'], plugin.handle_python)


if __name__ == '__main__':
    unittest.main()
