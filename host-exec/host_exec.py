"""Run an approved shell command on the host, with bounded output."""

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

OUTPUT_LIMIT = 65536  # bytes per stream
DESCRIPTION = ('Run an arbitrary host shell command in an absolute directory. '
               'Every call requests human approval. Output is not redacted; '
               'a timeout leaves side effects uncertain and must not be retried blindly.')
SCHEMA = {
    'name': 'host_exec', 'description': DESCRIPTION,
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'required': ['command'],
        'properties': {
            'command': {'type': 'string', 'description': 'Shell command passed to /bin/bash -c.'},
            'cwd': {'type': 'string', 'description': 'Absolute working directory; defaults to the current profile home.'},
            'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 600, 'default': 180},
        },
    },
}


def _profile_home() -> Path:
    """Resolve the active profile home at call time; no installation path is baked in."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def execute(args):
    home = _profile_home()
    if not isinstance(args, dict) or set(args) - {'command', 'cwd', 'timeout'}:
        raise ValueError('Expected command, cwd, and optional timeout only')
    command, cwd, timeout = args.get('command'), args.get('cwd', str(home)), args.get('timeout', 180)
    if not isinstance(command, str) or not command.strip() or '\0' in command:
        raise ValueError('command must be a nonempty shell string without NUL')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or '\0' in cwd:
        raise ValueError('cwd must be an absolute path without NUL')
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('timeout must be an integer from 1 to 600')

    # Preserve normal host authentication and environment; bash -c avoids login setup.
    proc = subprocess.Popen(['/bin/bash', '-c', command], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    truncated = False
    timed_out = False
    completed = False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for name in chunks:
                selector.register(getattr(proc, name), selectors.EVENT_READ, name)
            while selector.get_map() or proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    output = chunks[key.data]
                    room = OUTPUT_LIMIT - len(output)
                    output.extend(data[:room])
                    if len(data) > room:
                        truncated = True
        completed = True
    finally:
        # Kill the group on timeout or exceptional interruption, then reap the leader.
        if timed_out or not completed:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if not stream.closed:
                stream.close()
    result = {'exit_code': proc.returncode,
              'stdout': chunks['stdout'].decode('utf-8', errors='replace'),
              'stderr': chunks['stderr'].decode('utf-8', errors='replace'),
              'timed_out': timed_out, 'truncated': truncated}
    if timed_out:
        result['retry_safe'] = False
        result['note'] = 'Timed out; side effects are uncertain. Inspect state before any retry.'
    return result


def handle(args, **_):
    try:
        return json.dumps(execute(args), ensure_ascii=False)
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        return json.dumps({'exit_code': None, 'stdout': '', 'stderr': '',
                           'timed_out': False, 'truncated': False,
                           'error': str(exc)}, ensure_ascii=False)


def _pre_tool_call(tool_name='', args=None, **_):
    if tool_name != 'host_exec':
        return None
    return {'action': 'approve',
            'message': 'Host shell command requires human approval: ' + json.dumps(args, ensure_ascii=False),
            'rule_key': 'host_exec:' + uuid.uuid4().hex}


def register(ctx):
    ctx.register_hook('pre_tool_call', _pre_tool_call)
    ctx.register_tool(name='host_exec', toolset='host_exec', schema=SCHEMA,
                      handler=handle, description=DESCRIPTION)
