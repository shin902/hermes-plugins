"""Host Hermes administration CLI; conversation entry points fail closed."""
import json
import os
from pathlib import Path
import subprocess

HOME = Path('/home/shi/.hermes/profiles/coding')
ROOT = HOME.parent.parent
HERMES = '/home/shi/.hermes/hermes-agent/.hermes/bin/hermes'


def environment():
    # No inherited credentials, Python injection, PATH overrides, or launch-profile data.
    return {'HOME': '/home/shi', 'USER': 'shi', 'LOGNAME': 'shi',
            'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'HERMES_HOME': str(ROOT), 'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}


def validate(args):
    if not isinstance(args, dict):
        raise ValueError('Object with argv required')
    if set(args) != {'argv'}:
        raise ValueError('Unknown tool fields')
    argv = args.get('argv')
    if not isinstance(argv, list) or not all(isinstance(a, str) and '\x00' not in a for a in argv):
        raise ValueError('argv must be a list of NUL-free strings after the Hermes executable')
    guard_conversation(argv)
    # Preserve all selectors and values exactly; cross-profile administration is intentional.
    return list(argv)


# Recognition metadata, NOT an allowlist: unknown syntax is passed to Hermes.
# Snapshot from the installed _parser.py; no private runtime imports.
VALUE_OPTIONS = {'--usage-file', '--model', '--provider', '--reasoning', '--toolsets',
                 '--in', '--skills', '--resume', '--oneshot', '--profile'}
SWITCH_OPTIONS = {'--help', '--version', '--no-restore-cwd', '--worktree', '--accept-hooks',
                  '--yolo', '--pass-session-id', '--ignore-user-config', '--ignore-rules',
                  '--safe-mode', '--native', '--tui-native', '--dev', '--tui', '--cli'}
SHORT_OPTIONS = {'-m': '--model', '-t': '--toolsets', '-s': '--skills', '-r': '--resume',
                 '-z': '--oneshot', '-p': '--profile', '-c': '--continue',
                 '-w': '--worktree', '-h': '--help', '-V': '--version'}


def _option(token):
    """Recognize installed global syntax without rejecting future options."""
    flag, equal, value = token.partition('=')
    names = VALUE_OPTIONS | SWITCH_OPTIONS | {'--continue'}
    if flag.startswith('--') and flag not in names:
        matches = [name for name in names if name.startswith(flag)]
        if len(matches) == 1:
            flag = matches[0]  # argparse's unambiguous long abbreviation
    if flag in names:
        return flag, bool(equal)
    if token[:2] in SHORT_OPTIONS:
        return SHORT_OPTIONS[token[:2]], len(token) > 2
    return None, False


def _prefix(argv):
    """Return command index, introspection, oneshot, unknown-prefix marker.

    Stop at the first command/--. Never inspect admin payloads as global flags.
    Unknown option arity cannot be inferred safely; pass that invocation through
    instead of inventing a restriction or treating its value as a chat command.
    """
    i, inspect, oneshot = 0, False, False
    while i < len(argv):
        token = argv[i]
        if token == '--':
            return i + 1, inspect, oneshot, False
        if not token.startswith('-'):
            return i, inspect, oneshot, False
        flag, attached = _option(token)
        if flag is None:
            return i, inspect, oneshot, True
        inspect |= flag in {'--help', '--version'}
        oneshot |= flag == '--oneshot'
        if flag in VALUE_OPTIONS:
            i += 1 if attached else 2
        elif flag == '--continue':
            i += 1 if attached or i + 1 == len(argv) or argv[i + 1].startswith('-') else 2
        else:
            # Installed argparse supports short flag clusters (e.g. -wzPROMPT).
            if token.startswith('-') and not token.startswith('--') and len(token) > 2:
                index, help_seen, shot, unknown = _prefix(
                    [*argv[:i], token[:2], '-' + token[2:], *argv[i + 1:]])
                return (index - 1 if index >= i + 2 else index), help_seen, shot, unknown
            i += 1
    return i, inspect, oneshot, False


def guard_conversation(argv):
    def deny():
        raise PermissionError('Direct conversation/agent-launch route is not permitted by hermes_cli')

    # Launcher-level escape executes before CLI parsing. Arbitrary Python modules
    # can relaunch chat: the module runner is the one explicit passthrough exception.
    if argv[:1] == ['--run-module']:
        raise PermissionError('--run-module is excluded: arbitrary modules can bypass the chat guard')
    if argv[:1] == ['--print-runtime-command']:
        return
    index, introspection, oneshot, unknown = _prefix(argv)
    if introspection:
        return
    if oneshot:
        deny()
    if unknown:
        return
    if index >= len(argv):
        deny()
    command, tail = argv[index], argv[index + 1:]
    if command in {'chat', 'rl', 'desktop', 'gui', 'proxy', 'acp'}:
        # Help is only recognized as an option, not as an option's data value.
        if tail == ['--help'] or tail == ['-h']:
            return
        deny()
    if command in {'dashboard', 'serve'}:
        # Known value-taking runtime options: don't mistake --host=--status etc.
        i, lifecycle = 0, False
        while i < len(tail):
            flag, equal, value = tail[i].partition('=')
            if flag == '--':
                break
            if flag in {'--host', '--port', '--open-profile', '--ssh-session-token-file', '--ssh-owner-nonce'}:
                i += 1 if equal else 2
                continue
            if flag in {'--help', '-h', '--status', '--stop'} and not equal:
                lifecycle = True
            if command == 'dashboard' and i == 0 and tail[i] == 'register':
                return
            i += 1
        if not lifecycle:
            deny()
        return
    # Parent parsers only accept --accept-hooks before these subcommands.
    action_tail = list(tail)
    while action_tail[:1] == ['--accept-hooks']:
        action_tail.pop(0)
    if action_tail[:1] in (['--help'], ['-h']):
        return
    action = action_tail[0] if action_tail else None
    if command == 'gateway':
        if action in {None, 'run', 'start', 'restart', 'install'}:
            if action_tail[1:] in (['--help'], ['-h']):
                return
            deny()
        if action == 'migrate' and '--dry-run' not in action_tail:
            deny()  # convergence restarts the default backend
    if (command, action) in {('mcp', 'serve'), ('peer', 'dm'), ('peer', 'run'),
                             ('cron', 'run'), ('cron', 'tick'), ('curator', 'run')}:
        if action_tail[1:] not in (['--help'], ['-h']):
            deny()


def execute(args):
    from hermes_constants import get_hermes_home
    if Path(get_hermes_home()).resolve() != HOME:
        raise PermissionError('Only the coding profile may use this tool')
    argv = validate(args)
    try:
        result = subprocess.run([HERMES, *argv], env=environment(), cwd=HOME,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, errors='replace', timeout=600, shell=False)
        return {'stdout': result.stdout, 'stderr': result.stderr,
                'exit_code': result.returncode, 'timed_out': False}
    except subprocess.TimeoutExpired as exc:
        def text(value):
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
        return {'stdout': text(exc.stdout), 'stderr': text(exc.stderr),
                'exit_code': None, 'timed_out': True,
                'note': 'CLI timed out; mutation outcome unknown. Read status before retrying.'}


def administer(args, **kwargs):
    try:
        return json.dumps({'ok': True, **execute(args)})
    except (ValueError, TypeError, KeyError, PermissionError, OSError, RuntimeError,
            subprocess.TimeoutExpired) as exc:
        # Do not expose raw CLI stdout/stderr or input values.
        if isinstance(exc, subprocess.TimeoutExpired):
            message = 'Command timed out; mutation outcome unknown, read status before retrying'
        elif isinstance(exc, (OSError, TypeError, KeyError)):
            message = 'Host operation or input failed; details withheld'
        else:
            message = str(exc)
        return json.dumps({'ok': False, 'error': message})


def register(ctx):
    description = ('Pass arbitrary host Hermes CLI argv unchanged, including cross-profile '
                   'management and unknown/plugin commands and flags. Known direct chat/agent '
                   'launch routes are denied; help/status/stop administration, console and send '
                   'are allowed. Exception: launcher --run-module is excluded because arbitrary '
                   'Python modules bypass route classification. Unknown option arity and plugin '
                   'behavior are not a security boundary. Fixed executable, shell=False, '
                   'DEVNULL stdin, sanitized environment, 600-second timeout. Returns stdout, '
                   'stderr, exit_code, timed_out. No confirm. CLI output is not secret-redacted.')
    ctx.register_tool(name='hermes_cli', toolset='hermes-cli', handler=administer,
        description=description, schema={'name': 'hermes_cli', 'description': description,
        'parameters': {'type': 'object', 'additionalProperties': False,
        'required': ['argv'], 'properties': {
            'argv': {'type': 'array', 'items': {'type': 'string'},
                     'description': 'Hermes administration arguments, passed unchanged; cross-profile -p/--profile allowed. Chat/agent-launch routes are denied.'}}}})
