# Coding-profile OpenCode implementation worker

This plugin-local CLI is the external implementation worker for the Hermes
**coding profile only**. The profile's `omh_worker` tool calls it automatically
for `role="implementation"`. It does not use the shared OMO runtime and does not
change the parent, research, or review models.

## Integrated entry point

In the coding profile, call the `omh_worker` tool instead of this CLI directly:

```json
{
  "role": "implementation",
  "task": "Implement the requested change and run the focused tests.",
  "cwd": "/absolute/path/to/worktree",
  "timeout_seconds": 900
}
```

The caller supplies only a structured role and task. `role="implementation"`
(alias `builder`) selects the pinned OpenCode worker with an explicit absolute
`cwd`, a bounded timeout, and an optional `session_id` to resume. There is no
owner, model, provider, or executable argument, and no model fallback.
`action="select"` previews the selection without executing.

`role="research"`, `review`, or `parent` never starts OpenCode. `omh_worker`
returns a payload matching the exposed `delegate_task` schema —
`{"tasks": [{"goal": ..., "context": ...}]}` — for Hermes to dispatch with the
profile's existing delegation model configuration. It emits no singular `goal`
or `role` field, and OMH does not change that configuration. Unknown roles fail
closed, and the tool runs only when called — it does not intercept
`delegate_task`.

The worker sets both the OpenCode process cwd and `PWD` to the requested
directory, so edits cannot be redirected into the shell's current repository.
In a real handler run with `auto=False`, OpenCode's default permissions
permitted creating `hello.txt` in a fresh scratch directory and the on-disk
contents matched; no permission bypass is enabled by default.

The CLI below remains available for direct, explicit runs and diagnostics.

The worker pins the OpenCode model:

```text
opencode/deepseek-v4.1-flash-free
```

It launches OpenCode with `run --standalone --format json`, an explicit working
directory, a bounded timeout, `shell=False`, and OpenCode's own resumable
session id. It does not use `pi`, an automatic executor preference, or a global
worker-routing setting.

## Run

Use the installed coding-profile plugin path so the CLI runs from this source:

```bash
python3 "$HOME/.hermes/profiles/coding/plugins/omh/opencode_worker.py" \
  --cwd /absolute/path/to/worktree \
  --prompt 'Implement the requested change. Run the focused tests. Do not commit or push unless explicitly asked.'
```

`--cwd` is required and must be an existing absolute directory. The default
OpenCode binary is `$HOME/.opencode/bin/opencode`; `--opencode-bin` selects a
different absolute executable path without changing the pinned model.

The default timeout is 900 seconds. `--timeout` accepts 1 through 3600 seconds.
`--auto` is opt-in and passes OpenCode `--auto`; without it, the worker does not
auto-approve permissions.

## Resume

The result is one JSON object on stdout. Read `session_id` from it and pass it
back to continue the same OpenCode session:

```bash
python3 "$HOME/.hermes/profiles/coding/plugins/omh/opencode_worker.py" \
  --cwd /absolute/path/to/worktree \
  --session ses_example123 \
  --prompt 'Continue from the failing test.'
```

A requested session id that OpenCode answers with a different session id is
reported as a failure, because that means the resume did not happen.

Example success shape:

```json
{
  "auto": false,
  "cwd": "/absolute/path/to/worktree",
  "duration_seconds": 12.4,
  "exit_code": 0,
  "model": "opencode/deepseek-v4.1-flash-free",
  "ok": true,
  "opencode_exit_code": 0,
  "requested_session_id": "",
  "resumed": false,
  "schema_version": "omh_opencode_worker/v1",
  "session_id": "ses_example123",
  "text": "Implemented and tested.",
  "timed_out": false
}
```

## Failure behavior

- A non-zero OpenCode exit is returned as a non-zero CLI exit and `ok: false`;
  it is never rewritten as a success.
- Timeouts stop the private process group, set `timed_out: true`, and exit 124.
- An executor `error` or `session.error` JSON event fails the run even when
  OpenCode exits 0; a reported failure is never a successful turn.
- A `tool_use` part whose state is `error`, denied, rejected, or otherwise
  failed also fails the run even when OpenCode exits 0. This covers a permission
  denial that leaves OpenCode's exit code and stdout looking successful.
- Invalid JSON, a missing session id, or a mismatched resumed session id is a
  protocol failure with exit 1.
- A stream that exits 0 without a terminal `step_finish` whose
  `part.reason` is `stop` is a protocol failure with exit 1. OpenCode's finish
  reasons are `stop`, `length`, `tool-calls`, `content-filter`, `error`, and
  `unknown`; only `stop` is terminal success. `tool-calls` is a continuation,
  `length` is truncation, `content-filter`/`error` are failures, and
  `unknown` or a missing reason is not proof of completion. Text parts, an
  initial `step_start`, a session id, or a successful tool event are not
  completion evidence. A later `step_start` clears an earlier terminal finish,
  so a completed step cannot mask a later unfinished one.
- Validation failures print a JSON error and exit 2.

## Boundaries

The worker passes only the prompt, cwd, pinned model, and optional session id.
It does not forward the Hermes transcript, Hermes session id, parent model
configuration, or unrelated private context. It inherits the host environment
(needed for OpenCode configuration) but overrides `PWD` to the requested cwd. It also does not review, verify,
commit, push, or create merge evidence. Treat its output as implementation work
that still needs independent verification and review.

This worker is for the coding profile. Do not enable or invoke it from other
profiles, and do not replace it with `omo-runtime`, `pi`, or a model router when
the coding-profile implementation worker is requested.
