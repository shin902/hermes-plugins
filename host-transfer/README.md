# host-transfer

A Hermes plugin exposing one bounded `host_transfer(action, params)` tool for copying a
host file to an allowlisted remote over SSH. It follows the `host-github`/`host-ops`
pattern: unlike ordinary sandboxed terminal tools, the `ssh`/`scp` subprocesses run
directly on the host, so they work regardless of the configured Hermes terminal backend.

Only the Mac is supported for now; more named remotes can be added to `TARGETS`.

## Actions

| Action | Required params | Optional params | Effect |
|---|---|---|---|
| `probe` | none | `target` | Read-only SSH connectivity check (`echo ok`). |
| `send` | `local_path`, `confirm` | `target` | Copy one host file to the remote's Downloads folder, then verify its SHA-256. |

- `target` defaults to `mac`. The allowlist (`TARGETS`) fixes the SSH user, host, and
  remote directory (`/Users/shin/Downloads`); callers cannot choose an arbitrary host,
  user, path, or port.
- `local_path` must be an absolute path under `/home/shi`, resolving to an existing
  regular file. Symlinks are followed, so an escape outside `/home/shi` is rejected; SSH
  material under `/home/shi/.ssh` is refused.
- `send` requires `confirm: true` and reports `transfer_succeeded`, `verified`, and
  `retry_safe`. A successful `scp` whose remote checksum cannot be read or mismatches
  returns `verified: false`; do not blindly retry.

The tool builds fixed argument arrays and runs `subprocess.Popen(..., shell=False)` with a
minimal environment that does not inherit ambient credentials. It invokes
`ssh -F /dev/null` with explicit options (`BatchMode`, `ConnectTimeout`,
`StrictHostKeyChecking=accept-new`, a fixed `UserKnownHostsFile`, the fixed identity file,
`IdentitiesOnly=yes`) so an unreadable host/system SSH config cannot break the transfer.

## Trust and configuration

Paths, host, user, identity file, and the remote directory are constants specific to this
host. The trust boundary is `/home/shi`: any regular file there can be sent, so do not call
`send` on files the user has not asked to transfer. `confirm: true` is an input guard, not
human authorization on its own.

Audit metadata (`time`, validated action, target, success) is appended under this plugin
directory's `audit/calls.jsonl` with private directory/file permissions. Output is captured
by concurrent pipe-draining threads with a per-stream cap; timeouts terminate the process
group, then kill it. Errors and returned text are redacted.

## Activate (not executed here)

The plugin is installed for the `default` and `coding` profiles but must be enabled
explicitly, then Hermes restarted to load it:

```sh
hermes plugins enable host-transfer
hermes --profile coding plugins enable host-transfer
```

## Verify

```sh
python -m unittest discover -s host-transfer/tests -v
hermes plugins doctor host-transfer --ci
```

Tests use mocked child processes and a real temporary file; they make no network requests
and copy nothing. A live `probe` (`params: {"target": "mac"}`) only succeeds when the Mac
is reachable and `~/.ssh/id_ed25519_mac` is authorized.
