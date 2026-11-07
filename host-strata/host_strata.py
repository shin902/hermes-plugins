"""Build, test and serve a local Strata checkout on the host.

Every long-running step is a transient systemd *user* unit, so stopping a unit
kills its whole cgroup (engine children included) and the exit status stays
readable afterwards. This tool runs without human approval.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

SERVE = 'host-strata-serve'
JOB = 'host-strata-job'
UNITS = {'serve': SERVE, 'job': JOB}
PROPS = ('ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'ExecMainPID',
         'ExecMainStartTimestamp', 'ExecMainExitTimestamp')
SAFE_NAME = re.compile(r'^[A-Za-z0-9._-]+$')
SAFE_TARGET = re.compile(r'^[A-Za-z0-9._+-]+$')
RUNNING_STATES = ('active', 'activating', 'deactivating')
LOG_TAIL_BYTES = 256 * 1024
REQUIRED_CONFIG = {'repo': str, 'proxy_service': str, 'port': int}
DESCRIPTION = (
    'Build, test and serve a local Strata checkout on the host. Runs WITHOUT human '
    'approval: configure/build/test execute checkout code and load the engine config as '
    'the host user, so anyone able to write either can run code on the host. Units are '
    'transient systemd --user jobs; the tool never blocks on a build or a server.')
SCHEMA = {
    'name': 'host_strata', 'description': DESCRIPTION,
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'required': ['action'],
        'properties': {
            'action': {'type': 'string', 'enum': [
                'status', 'proxy_stop', 'proxy_start', 'serve_start', 'serve_stop',
                'configure', 'build', 'test', 'job_stop', 'log']},
            'checkout': {'type': 'string', 'description': 'Absolute checkout path; defaults to the configured repo. Worktrees of it are allowed.'},
            'build_dir': {'type': 'string', 'description': 'Build directory name, relative to the checkout; defaults to the configured build_dir.'},
            'engine_config': {'type': 'string', 'description': 'Absolute engine JSON inside an allowed config dir; defaults to default_engine_config.'},
            'target': {'type': 'string', 'description': 'Optional cmake target for build.'},
            'suite': {'type': 'string', 'enum': ['ctest', 'serve']},
            'filter': {'type': 'string', 'description': 'ctest -R regex, or unittest discovery pattern for the serve suite.'},
            'unit': {'type': 'string', 'enum': ['serve', 'job'], 'default': 'job'},
            'lines': {'type': 'integer', 'minimum': 1, 'maximum': 400, 'default': 80},
        },
    },
}


def run(argv, timeout=60):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _root():
    """Default HERMES_HOME, resolved at call time (never baked in)."""
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    return home.parent.parent if home.parent.name == 'profiles' else home


def _state_dir():
    return _root() / 'plugin-state' / 'host-strata'


def _logs_dir():
    return _state_dir() / 'logs'


def _record_path(unit):
    return _state_dir() / (unit + '.json')


def load_config():
    path = _root() / 'plugin-config' / 'host-strata' / 'config.yaml'
    if not path.is_file():
        raise ValueError('missing config ' + str(path))
    import hermes_yaml as yaml

    data = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('config must be a mapping')
    for key, kind in REQUIRED_CONFIG.items():
        if not isinstance(data.get(key), kind):
            raise ValueError(f'config {key} must be a {kind.__name__}')
    return data


def unit_state(name):
    result = run(['systemctl', '--user', 'show', name, '-p', ','.join(PROPS)])
    state = {'unit': name}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        if sep and key in PROPS:
            state[key] = value
    return state


def active(name):
    state = unit_state(name)
    return (state.get('ActiveState') in RUNNING_STATES
            and state.get('SubState') != 'exited')


def finished(state):
    return state.get('SubState') == 'exited' or state.get('ActiveState') == 'failed'


def job_view(state):
    """Unit state plus the exit code of the last run, once it has finished."""
    view = dict(state)
    view['finished'] = finished(state)
    if view['finished']:
        try:
            view['exit_code'] = int(state.get('ExecMainStatus', ''))
        except (TypeError, ValueError):
            view['exit_code'] = None
    return view


def read_record(unit):
    try:
        return json.loads(_record_path(unit).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def wait_until(predicate, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def launch(unit, argv, cwd, env, remain_after_exit, extra=None):
    run(['systemctl', '--user', 'stop', unit])
    run(['systemctl', '--user', 'reset-failed', unit])
    logs = _logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / (unit + '-' + time.strftime('%Y%m%d-%H%M%S') + '.log')
    command = ['systemd-run', '--user', '--unit', unit, '--quiet',
               '-p', 'WorkingDirectory=' + str(cwd),
               '-p', 'StandardOutput=append:' + str(log),
               '-p', 'StandardError=append:' + str(log),
               '-p', 'TimeoutStopSec=60']
    if remain_after_exit:
        command += ['-p', 'RemainAfterExit=yes']
    for key, value in env.items():
        command += ['--setenv', f'{key}={value}']
    result = run(command + ['--', *argv])
    if result.returncode:
        raise ValueError('systemd-run failed: '
                         + (result.stderr.strip() or f'exit {result.returncode}'))
    record = {'unit': unit, 'argv': list(argv), 'cwd': str(cwd), 'log': str(log),
              'started': time.strftime('%Y-%m-%dT%H:%M:%S'), **(extra or {})}
    _state_dir().mkdir(parents=True, exist_ok=True)
    _record_path(unit).write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def resolve_checkout(cfg, arg):
    if arg is not None and not os.path.isabs(arg):
        raise ValueError('checkout must be an absolute path')
    repo = Path(os.path.realpath(cfg['repo']))
    path = Path(os.path.realpath(arg or repo))
    if path == repo:
        return path
    listing = run(['git', '-C', str(repo), 'worktree', 'list', '--porcelain']).stdout
    trees = {os.path.realpath(line.split(' ', 1)[1]) for line in listing.splitlines()
             if line.startswith('worktree ') and ' ' in line}
    if str(path) not in trees:
        raise ValueError(f'checkout {path} is neither {repo} nor one of its worktrees')
    return path


def resolve_build_dir(cfg, checkout, name):
    name = name or str(cfg.get('build_dir') or 'build')
    if not SAFE_NAME.match(name) or name in ('.', '..'):
        raise ValueError('build_dir must be a plain directory name')
    return Path(checkout) / name


def resolve_engine_config(cfg, arg):
    raw = arg or cfg.get('default_engine_config')
    if not raw:
        raise ValueError('engine_config is required')
    if not os.path.isabs(str(raw)):
        raise ValueError('engine_config must be an absolute path')
    path = Path(os.path.realpath(str(raw)))
    if path.suffix != '.json' or not path.is_file():
        raise ValueError(f'engine_config must be an existing .json file: {path}')
    for directory in cfg.get('engine_config_dirs') or []:
        if path.is_relative_to(Path(os.path.realpath(str(directory)))):
            return path
    raise ValueError(f'engine_config {path} is outside the allowed config dirs')


def python_path(cfg):
    path = Path(str(cfg.get('python') or '.venv/bin/python'))
    return path if path.is_absolute() else Path(os.path.realpath(cfg['repo'])) / path


def expand(value, checkout):
    return str(value).replace('{checkout}', str(checkout))


def unit_env(cfg, checkout):
    return {str(k): expand(v, checkout) for k, v in (cfg.get('env') or {}).items()}


def test_env(cfg, checkout):
    env = unit_env(cfg, checkout)
    # Units start from the systemd user manager's environment, not Hermes's.
    libs = [expand(d, checkout) for d in cfg.get('lib_dirs') or []]
    if libs:
        env['LD_LIBRARY_PATH'] = ':'.join(libs)
    return env


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def provenance(checkout, engine_config):
    """What exactly is being served, frozen at launch for benchmark records."""
    def git(*argv):
        return run(['git', '-C', str(checkout), *argv]).stdout.strip()
    exe = json.loads(engine_config.read_text(encoding='utf-8')).get('exe')
    return {'git_head': git('rev-parse', 'HEAD'),
            'git_describe': git('describe', '--tags', '--always', '--dirty'),
            'engine_config': str(engine_config),
            'engine_config_sha256': sha256_file(engine_config),
            'exe': exe,
            'exe_sha256': sha256_file(exe) if exe and os.path.isfile(exe) else None}


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def act_status(cfg, args):
    proxy = cfg['proxy_service']
    proxy_state = unit_state(proxy)
    return {'proxy': {'service': proxy,
                      'ActiveState': proxy_state.get('ActiveState'),
                      'SubState': proxy_state.get('SubState')},
            'serve': {**unit_state(SERVE), 'record': read_record(SERVE),
                      'health': health(cfg['port'])},
            'job': job_view(unit_state(JOB))}


def act_proxy_stop(cfg, args):
    proxy = cfg['proxy_service']
    run(['systemctl', '--user', 'stop', proxy])
    wait_until(lambda: unit_state(proxy).get('ActiveState') in ('inactive', 'failed'), 60)
    return unit_state(proxy)


def act_proxy_start(cfg, args):
    proxy = cfg['proxy_service']
    run(['systemctl', '--user', 'start', proxy])
    wait_until(lambda: active(proxy), 30)
    return unit_state(proxy)


def act_serve_start(cfg, args):
    proxy = cfg['proxy_service']
    if active(proxy):
        raise ValueError('stop the proxy first (proxy_stop)')
    if active(SERVE):
        raise ValueError('serve is already running')
    checkout = resolve_checkout(cfg, args.get('checkout'))
    engine_config = resolve_engine_config(cfg, args.get('engine_config'))
    port = cfg['port']
    record = launch(SERVE, [str(python_path(cfg)), 'serve/server.py', '--engine', 'strata',
                            '--config', str(engine_config), '--port', str(port)],
                    checkout, unit_env(cfg, checkout), remain_after_exit=False,
                    extra={'provenance': provenance(checkout, engine_config)})
    return {**record, 'url': f'http://127.0.0.1:{port}'}


def act_serve_stop(cfg, args):
    run(['systemctl', '--user', 'stop', SERVE])
    run(['systemctl', '--user', 'reset-failed', SERVE])
    return unit_state(SERVE)


def act_configure(cfg, args):
    if active(JOB):
        raise ValueError('a job is already running (job_stop)')
    checkout = resolve_checkout(cfg, args.get('checkout'))
    build_path = resolve_build_dir(cfg, checkout, args.get('build_dir'))
    if (build_path / 'CMakeCache.txt').exists():
        raise ValueError(f'build dir already configured: {build_path}')
    cmake_args = [expand(a, checkout) for a in cfg.get('cmake_args') or []]
    return launch(JOB, ['cmake', '-S', str(checkout), '-B', str(build_path), *cmake_args],
                  checkout, unit_env(cfg, checkout), remain_after_exit=True)


def act_build(cfg, args):
    if active(JOB):
        raise ValueError('a job is already running (job_stop)')
    checkout = resolve_checkout(cfg, args.get('checkout'))
    build_path = resolve_build_dir(cfg, checkout, args.get('build_dir'))
    if not (build_path / 'CMakeCache.txt').exists():
        raise ValueError(f'build dir is not configured: {build_path}')
    target = args.get('target')
    if target is not None and not SAFE_TARGET.match(target):
        raise ValueError('target has invalid characters')
    argv = ['cmake', '--build', str(build_path), '-j', str(int(cfg.get('build_jobs') or 8))]
    if target:
        argv += ['--target', target]
    return launch(JOB, argv, checkout, unit_env(cfg, checkout), remain_after_exit=True)


def act_test(cfg, args):
    suite = args.get('suite')
    if suite not in ('ctest', 'serve'):
        raise ValueError('suite must be ctest or serve')
    if active(JOB):
        raise ValueError('a job is already running (job_stop)')
    if suite == 'ctest' and active(SERVE):
        raise ValueError('stop serve first: ctest shares the GPU')
    checkout = resolve_checkout(cfg, args.get('checkout'))
    pattern = args.get('filter')
    if pattern is not None and len(pattern) > 200:
        raise ValueError('filter must be at most 200 characters')
    if suite == 'ctest':
        build_path = resolve_build_dir(cfg, checkout, args.get('build_dir'))
        argv = ['ctest', '--test-dir', str(build_path), '--output-on-failure']
        if pattern:
            argv += ['-R', pattern]
    else:
        argv = [str(python_path(cfg)), '-m', 'unittest', 'discover',
                '-s', 'serve', '-t', '.', '-p', pattern or 'test_*.py']
    return launch(JOB, argv, checkout, test_env(cfg, checkout), remain_after_exit=True)


def act_job_stop(cfg, args):
    run(['systemctl', '--user', 'stop', JOB])
    return job_view(unit_state(JOB))


def act_log(cfg, args):
    unit = UNITS[args.get('unit') or 'job']
    state = unit_state(unit)
    record = read_record(unit) or {}
    path = record.get('log')
    result = {'unit': unit, 'log': path, 'state': job_view(state), 'lines': []}
    if not path:
        result['note'] = 'no recorded log for this unit'
        return result
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode('utf-8', errors='replace')
    except OSError as exc:
        result['error'] = f'cannot read {path}: {exc}'
        return result
    lines = text.splitlines()
    if size > LOG_TAIL_BYTES and lines:
        lines = lines[1:]  # first line is likely cut mid-way
    result['lines'] = lines[-int(args.get('lines') or 80):]
    return result


ACTIONS = {'status': act_status, 'proxy_stop': act_proxy_stop,
           'proxy_start': act_proxy_start, 'serve_start': act_serve_start,
           'serve_stop': act_serve_stop, 'configure': act_configure,
           'build': act_build, 'test': act_test, 'job_stop': act_job_stop,
           'log': act_log}


PYTHON_DESCRIPTION = (
    "Run the Strata checkout's .venv Python on the host and wait for it, WITHOUT human "
    "approval: arbitrary code as the host user (pip installs, tools/*.py, scripts). "
    "args go after the interpreter, e.g. ['-m', 'pip', 'install', '-r', 'requirements.txt'].")
PYTHON_SCHEMA = {
    'name': 'python_cli', 'description': PYTHON_DESCRIPTION,
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'required': ['args'],
        'properties': {
            'args': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1,
                     'description': 'Arguments after the .venv python interpreter.'},
            'checkout': SCHEMA['parameters']['properties']['checkout'],
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600, 'default': 600},
        },
    },
}
OUTPUT_TAIL_CHARS = 64 * 1024


def python_cli(cfg, args):
    """A transient unit with --wait --pipe: same clean env as the other units, its
    cgroup is killed at RuntimeMaxSec, and the exit status comes back directly."""
    argv = args['args']
    if not isinstance(argv, list) or not argv or not all(
            isinstance(a, str) and '\0' not in a for a in argv):
        raise ValueError('args must be a non-empty list of strings without NUL')
    timeout = args.get('timeout', 600)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('timeout must be an integer from 1 to 3600')
    checkout = resolve_checkout(cfg, _text(args, 'checkout'))
    command = ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--quiet',
               '-p', f'WorkingDirectory={checkout}', '-p', f'RuntimeMaxSec={timeout}']
    for key, value in test_env(cfg, checkout).items():
        command += ['--setenv', f'{key}={value}']
    started = time.monotonic()
    result = subprocess.run(command + ['--', str(python_path(cfg)), *argv],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=timeout + 30)
    # systemd kills the unit at RuntimeMaxSec and reports a bare failure.
    timed_out = result.returncode != 0 and time.monotonic() - started >= timeout
    return {'exit_code': result.returncode, 'timed_out': timed_out, 'cwd': str(checkout),
            'output': result.stdout[-OUTPUT_TAIL_CHARS:],
            'truncated': len(result.stdout) > OUTPUT_TAIL_CHARS}


def _text(args, key):
    value = args.get(key)
    if value is not None and (not isinstance(value, str) or '\0' in value):
        raise ValueError(f'{key} must be a string without NUL')
    return value


def execute(args):
    if not isinstance(args, dict):
        raise ValueError('expected an object of arguments')
    unknown = set(args) - set(SCHEMA['parameters']['properties'])
    if unknown:
        raise ValueError('unknown argument(s): ' + ', '.join(sorted(unknown)))
    action = args.get('action')
    if action not in ACTIONS:
        raise ValueError('action must be one of: ' + ', '.join(ACTIONS))
    for key in ('checkout', 'build_dir', 'engine_config', 'target', 'filter'):
        _text(args, key)
    if args.get('suite') is not None and args['suite'] not in ('ctest', 'serve'):
        raise ValueError('suite must be ctest or serve')
    if args.get('unit', 'job') not in UNITS:
        raise ValueError('unit must be serve or job')
    lines = args.get('lines', 80)
    if type(lines) is not int or not 1 <= lines <= 400:
        raise ValueError('lines must be an integer from 1 to 400')
    return ACTIONS[action](load_config(), args)


def _respond(call):
    try:
        return json.dumps(call(), ensure_ascii=False)
    except (ValueError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({'error': str(exc)}, ensure_ascii=False)


def handle(args, **_):
    return _respond(lambda: execute(args))


def execute_python(args):
    if not isinstance(args, dict):
        raise ValueError('expected an object of arguments')
    unknown = set(args) - set(PYTHON_SCHEMA['parameters']['properties'])
    if unknown:
        raise ValueError('unknown argument(s): ' + ', '.join(sorted(unknown)))
    return python_cli(load_config(), args)


def handle_python(args, **_):
    return _respond(lambda: execute_python(args))


def register(ctx):
    ctx.register_tool(name='host_strata', toolset='host_strata', schema=SCHEMA,
                      handler=handle, description=DESCRIPTION)
    ctx.register_tool(name='python_cli', toolset='host_strata', schema=PYTHON_SCHEMA,
                      handler=handle_python, description=PYTHON_DESCRIPTION)
