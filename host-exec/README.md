# host-exec

`host_exec` runs an arbitrary host command as the user running Hermes, using `/bin/bash -c` and the inherited host environment. Existing host authentication, including `gh`, is available to the extent it is available to that user. This is not a sandbox or an action allowlist: an approved command can read or change anything that user can access.

Every call requests human approval via the public `pre_tool_call` hook with a fresh approval key. Hermes off/YOLO and unattended approval policies can bypass the standard gate. The handler resolves the calling profile's home at call time through the documented Hermes home helper, so it works in any profile where the plugin is enabled. Do not expose this plugin to untrusted agents or mistake an agent confirmation for human approval.

## Input

- `command` (required): nonempty shell command string.
- `cwd` (optional): absolute working directory; defaults to the calling profile's Hermes home.
- `timeout` (optional): integer seconds from 1 to 600; default 180.

Example: `{"command":"gh auth status", "cwd":"/home/shi", "timeout":30}`

## Result

`exit_code` is the shell process return code (negative means terminated by a signal), `stdout` and `stderr` are UTF-8 text with replacement for invalid bytes, `timed_out` indicates the deadline was reached, and `truncated` indicates either stream exceeded its 65,536-byte limit. Excess output is drained and discarded. On timeout, the process group is killed and `retry_safe` is false: side effects may already have occurred, so inspect state before retrying. A shell that exits while a descendant retains its pipes still waits until the deadline, then its process group is killed. Processes that deliberately escape the process group are not contained.

Output is **not redacted**; commands can expose secrets in output. Validation or startup errors return an `error` with null `exit_code`. A successful exit does not verify external side effects.

## Checks

Run `python3 -m unittest discover -s host-exec/tests -v` from the repository root, and `hermes plugins validate host-exec` / `hermes plugins doctor host-exec --ci` when the Hermes CLI is available. These checks do not activate the plugin or prove a live approved host call.
