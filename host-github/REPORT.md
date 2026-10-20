# host-github verification report

## Changes

- Removed `pr_comment` and `issue_comment` from the action set and schema. V1 does not attempt comment writes without exact comment readback.
- Kept PR and issue creation behind `confirm: true`, then verify returned target identity and exact title/body; PRs also verify head/base.
- Write responses report `write_succeeded`, `verified`, and `retry_safe`. Unknown write outcomes use `null/false/false`; failed or mismatched readback uses `true/false/false`.
- Replaced buffered `subprocess.run` output with concurrent, bounded `Popen` pipe draining and timeout terminate/kill/reap handling.
- Hardened enum and label validation, accepted statuses listed by local `gh run list --help`, redacted successful text without truncating JSON, and restricted audit fields to validated values.
- Documented action-specific allowed and required parameters in the schema and README.

## Verification

- `python -m unittest discover -s plugins/host-github/tests -v`: **17 tests passed**. The output and timeout tests use local subprocess fixtures; no network or GitHub writes are involved.
- `python -m py_compile plugins/host-github/host_github.py plugins/host-github/tests/test_host_github.py`: passed.
- `/usr/bin/gh run list --help`: confirmed supported statuses, including `success`, `failure`, `startup_failure`, and `timed_out`.
- Plugin Doctor could not reach command handling because the sandbox denied access to the Hermes install/prepare lock files. This is an install-lock sandbox restriction, not a host plugin failure.
- The prior read-only `repo_view NousResearch/hermes-agent` host smoke check passed. Network/auth restrictions reported by sandboxed checks are environmental restrictions, not host failures.
- Docker end-to-end behavior remains untested. No GitHub writes were performed and the plugin was not enabled.
