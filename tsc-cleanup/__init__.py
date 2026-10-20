"""Temporary post-tool cleanup using only the public hook API and Docker CLI."""

import json
import logging
import subprocess
import threading

LOGGER = logging.getLogger("tsc-cleanup")
CLEANUP_LOCK = threading.Lock()

CONTAINER_SCRIPT = r'''
import json
import os
from pathlib import Path
import signal
import time

compilers = []
for directory in Path('/proc').iterdir():
    if not directory.name.isdigit():
        continue
    try:
        arguments = (directory / 'cmdline').read_bytes().split(b'\0')
        if not arguments or Path(os.fsdecode(arguments[0])).name not in ('node', 'nodejs'):
            continue
        if len(arguments) < 2:
            continue
        script = os.fsdecode(arguments[1])
        if not script.endswith(('/typescript/bin/tsc', '/typescript/lib/tsc.js', '/.bin/tsc')):
            continue
        descriptor = os.pidfd_open(int(directory.name))
        if (directory / 'cmdline').read_bytes().split(b'\0') != arguments:
            os.close(descriptor)
            continue
        compilers.append((int(directory.name), descriptor))
    except (FileNotFoundError, ProcessLookupError):
        continue

for pid, descriptor in compilers:
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    except ProcessLookupError:
        pass
if compilers:
    time.sleep(0.5)
for pid, descriptor in compilers:
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)
print(json.dumps({'terminated_container_pids': [pid for pid, descriptor in compilers]}))
'''


def cleanup_coding_compilers(**kwargs):
    """Sweep coding containers after any tool, including manually launched checks."""
    if not CLEANUP_LOCK.acquire(blocking=False):
        return
    try:
        listing = subprocess.run(
            ['docker', 'ps', '--filter', 'label=hermes-agent=1',
             '--filter', 'label=hermes-profile=coding', '--format', '{{.ID}}'],
            capture_output=True, text=True, timeout=3, check=True,
        )
        for container_id in listing.stdout.splitlines():
            if not container_id or any(character not in '0123456789abcdef' for character in container_id):
                continue
            cleanup = subprocess.run(
                ['docker', 'exec', '-i', container_id, 'python3', '-'],
                input=CONTAINER_SCRIPT, capture_output=True, text=True, timeout=5, check=True,
            )
            report = json.loads(cleanup.stdout)
            if report['terminated_container_pids']:
                LOGGER.warning('Stopped tsc in coding container %s: %s', container_id, report)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        LOGGER.warning('Coding tsc cleanup failed: %s', error)
    finally:
        CLEANUP_LOCK.release()


def register(ctx):
    ctx.register_hook('post_tool_call', cleanup_coding_compilers)
