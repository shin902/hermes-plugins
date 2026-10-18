# host-ops change report

## Scope

Only `plugins/host-ops` was changed. The existing `plugin.yaml` was preserved. No configuration writes, service changes, cron mutations, device operations, installs, or changes to other profiles/plugins were performed.

## Changes

- `config_set` schema and validator require `confirm: true`.
- Validate action and parameter types before assigning the action for audit; malformed arrays/objects produce JSON errors. Unit and endpoint validation handles unhashable values safely.
- Read-only cron/list/status/runs and service logs derive top-level success from the process exit status.
- Cron accepts an optional allowlisted profile for cron actions only, defaults to `coding`, checks the profile directory, and uses that same profile for invocation and registry readback. Config actions remain coding-only. Registry readback includes scheduling and last-run fields.
- Cron run verification recognizes the installed CLI's explicit synchronous completion, background, or next-tick output semantics; a generic successful exit is insufficient.
- Docker inspection requires both `hermes-agent=1` and the exact requested profile label.
- Service stop requires exact `inactive`; daemon reload reports execution without claiming independent verification.
- Recursive output redaction covers bearer credentials and `gho_` / `github_pat_` credentials without arbitrary truncation.
- Subprocess timeout now covers process waiting and pipe draining, kills the process group when descendants retain pipes, and has a real child-process regression test.
- README distinguishes sandbox fixtures and results from parent-reported live host observations.

## Verification

- `python -m unittest discover -s plugins/host-ops/tests -v`: 18 tests passed after the changes.
- Plugin doctor was attempted in the sandbox but Hermes could not acquire/write `/home/shi/.hermes/installs/.../.prepare.lock` and `.install.lock` because the filesystem is read-only. Parent-reported host doctor succeeds; host smoke results are documented in `README.md` and were not rerun here.
- Parent reports the host-context plugin doctor passes. Parent-reported live read-only results: `hermes-logbook-capture.service` active; `cron_status` exit 0; `config_get terminal.backend` = `local`; two managed Docker containers; logbook health HTTP 200.
