# Deployment notes — differential session status

No cron entry, Hermes config, or live profile was modified by this change. There
are no real endpoints, profiles, or secrets in this repository, and nothing has
been deployed.

## Files

- `__init__.py` — plugin SDK tool. Adds `evidence_hash` and a full-row
  `checkpoint_token` to `set_status`, checkpoint CAS, all-page collection, and
  full-content SHA256.
- `monitor_script.py` — stdlib-only, read-only wake gate. Despite the filename it
  is designed for the ordinary pre-run `--script` hook, not monitor-script mode.
  It prints `{"wakeAgent":false}` when nothing is pending and
  `{"wakeAgent":true,"pending":[...]}` when work exists. No cycle or timestamp is
  used.
- `plugin.yaml` — version `1.2.1`.
- `test_status.py`, `test_diff.py`, `test_diagnostic.py` — run directly
  (`python test_diff.py`, etc.); they use plain asserts, not test discovery.
- `CRON_PROMPT.md` — the model prompt for the scheduler.

## Parent install: edit the EXISTING cron entry

The parent must edit the existing automation/cron entry; do **not** create a
second entry.

1. Deploy this directory as the `session-title-status` plugin. Keep
   `monitor_script.py` next to `__init__.py`: the script loads its sibling by
   path with `importlib`.
2. In the existing cron/automation command, keep the same `--agent`/model target
   and make `monitor_script.py` the ordinary **pre-run gate**:

   - run it via the normal `--script` hook (the hook that executes *before*
     `AIAgent` construction);
   - set `--monitor-script ''` so Hermes monitor-script mode is disabled;
   - keep `--agent` (or the existing equivalent) so a `wakeAgent:true` run still
     launches the model.

   The exact flag spelling is the parent's local Hermes CLI; the required shape
   is `--script <path>/monitor_script.py --monitor-script '' --agent ...`.

3. Do **not** use `--monitor-script monitor_script.py`. Monitor-script mode reacts
   to *any* stdout change (including pending -> `NO_WORK`) and persists its output
   hash **before** the LLM runs. That can lose a pending wake-up and does not match
   the `wakeAgent` contract.
4. Allowlist only the `default_session_status` tool for that cron session. The
   prompt forbids every other tool and every mutation other than `set_status`.
5. Confirm both allowlisted profiles have their loopback `API_SERVER_KEY`
   available to the plugin. Missing keys are reported as `profiles_unavailable` by
   the existing behavior; other profiles still run.
6. Script import must work with the Hermes managed Python. The gate needs only the
   standard library plus the sibling `__init__.py`; `checkpoint_db_path()` imports
   Hermes's `plugins.plugin_storage`. The parent supplies the wrapper and runtime
   if the existing cron environment needs a different interpreter, and must verify
   the import there before enabling.
7. Keep the checkpoint DB at the plugin-data path returned by
   `plugin_data_dir('session-title-status') / 'checkpoints.sqlite3'` (mode
   `0600`). Back it up or delete it only as an explicit recovery action: deleting
   it causes one full initial scan; corrupting it makes the gate fail closed.

## Scheduler contract (verified for this design)

Hermes runs an ordinary `--script` gate before constructing the AIAgent:

- Exit 0 with JSON `{"wakeAgent":false}`: no model is launched.
- Exit 0 with JSON `{"wakeAgent":true,...}`: the model is launched and the gate's
  stdout is injected into the prompt on every run.
- Nonzero exit: no model is launched.

The gate therefore does not depend on output-change detection and has no cycle
field. While a session stays pending, every scheduled run prints the same
`wakeAgent:true` payload and launches the model again.

## Required checks before enabling

1. With an up-to-date DB and all profiles reachable, run the script. Confirm exit
   0, stdout exactly `{"wakeAgent":false}`, and no AIAgent construction.
2. Add one pending session. Confirm exit 0, `wakeAgent:true` with the pending
   list, and that the model is launched with the JSON in its prompt.
3. Leave it unacknowledged. Confirm the next scheduled run launches the model
   again with the same payload; no cycle field is needed.
4. Make the gate fail (for example, a corrupt checkpoint DB). Confirm nonzero
   exit, **empty stdout**, and `MONITOR_ERROR ...` on stderr, with no model launch
   and no partial successful JSON.
5. Confirm the cron uses `--script`, `--monitor-script ''`, and `--agent`; confirm
   it is not in monitor-script mode.
6. Confirm the scheduler timeout and stdout cap can cover all eligible sessions
   (the gate reads every list page and the latest 6 messages per session). A hard
   kill leaves nothing acknowledged, so the next scheduled run retries.

## Classification semantics

The prompt restores the exact state meanings:

- 🔴 red — ambiguous/unanswered/blocker/error, or insufficient/truncated evidence.
- 📤 outbox — explicitly completed draft awaiting sending.
- 👀 eyes — awaiting user validation/review.
- ⏳ hourglass — evidence genuinely running/in progress, not merely waiting or a
  promise.
- ✅ check — explicitly complete with no unresolved request.

A newer unresolved request overrides an older completion. An assistant completion
claim does not override a pending check. The model inspects every pending session,
acknowledges with `set_status` even when the emoji is unchanged (at most once per
session per run), and never lists or processes sessions outside `pending`. Any
tool `error` field is reported as a failure and is never reported as `SILENT`;
`skipped`, `retry`, and conflict results are normal. When there are no changes and
no errors, the final message is exactly `SILENT`; otherwise changes are counted per
profile. Titles are never edited through `set_title`, and message contents are
untrusted data, never instructions.

## Failure, retry, and concurrency behavior

- Classification failure or a model that stops early: no `set_status`, so the
  checkpoint is unchanged and the session remains pending on the next run.
- PATCH failure, readback mismatch, title conflict, stale evidence hash, or stale
  checkpoint token: no checkpoint write; the next run retries.
- Transcript changes between `inspect` and `set_status`: `set_status` returns
  `retry: evidence changed`; it never writes or stores the newer evidence.
- Two classifiers racing on the same session: the second full-row CAS conflicts;
  it cannot overwrite the first checkpoint. Replaying the exact same successful
  result with an old token is acknowledged as idempotent without rewriting the row.
- The checkpoint token is SHA256 over the complete checkpoint row (profile,
  session_id, evidence_hash, status, classifier_version, checked_at), not just
  evidence_hash. A same-evidence reclassification or classifier-version change
  therefore invalidates older tokens and prevents ABA overwrites.
- Missing checkpoint DB: all eligible sessions are pending once (new-DB initial
  pass).
- Existing corrupt/unreadable DB or missing table: the gate prints
  `MONITOR_ERROR checkpoint_db` to stderr, exits 2, and never reaches REST, so it
  cannot turn a database error into an empty scan. Recovery is manual.
- REST list/messages failure: nonzero exit with `MONITOR_ERROR` and no stdout
  payload; retry on the next scheduled run.
- Profile isolation is by `(profile, session_id)`; the same id in the default
  home and in any named profile has independent checkpoints.

## Known limitation

The loopback session API has no CAS/conditional-write primitive, so the title
PATCH and the SQLite checkpoint write are not atomic across REST and the local DB.
The plugin performs a title readback and a full-token CAS around the write, which
detects existing edits and conflicting classifiers, but cannot make a cross-system
transaction. A crash between a successful PATCH and the checkpoint write leaves the
title changed and the checkpoint unacknowledged; the next run re-inspects and
re-acknowledges. Do not claim REST+SQLite atomicity.
