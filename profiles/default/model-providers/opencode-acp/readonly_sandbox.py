"""Linux filesystem read-only boundary; networking deliberately remains enabled."""
import os
import shutil
from pathlib import Path

def sandbox_argv(command, args, cwd):
    bwrap = shutil.which('bwrap')
    executable = shutil.which(command) or (command if Path(command).is_absolute() and Path(command).is_file() else None)
    if not bwrap or not executable:
        raise RuntimeError('Read-only ACP requires bubblewrap and a resolvable OpenCode executable')
    cwd = str(Path(cwd).resolve(strict=True))
    argv = [bwrap, '--die-with-parent', '--new-session', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL', '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/var/tmp', '--tmpfs', '/run']
    # Re-expose the requested workspace before masking secrets; a workspace
    # may not undo masks by being an ancestor of a protected directory.
    if cwd != '/':
        argv += ['--ro-bind', cwd, cwd]
    # Hide credentials/configuration without changing the user's normal CLI.
    home = Path.home()
    for leaf in ('.hermes', '.config', '.local/share/opencode', '.cache/opencode', '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.claude', '.agents', '.netrc', '.npmrc'):
        path = home / leaf
        if path.is_dir():
            argv += ['--tmpfs', str(path)]
        elif path.is_file():
            argv += ['--ro-bind', '/dev/null', str(path)]
    argv += ['--dir', '/tmp/acp-home', '--dir', '/tmp/acp-config', '--dir', '/tmp/acp-data', '--dir', '/tmp/acp-state', '--dir', '/tmp/acp-cache', '--chdir', cwd, '--', executable, *args]
    return argv
