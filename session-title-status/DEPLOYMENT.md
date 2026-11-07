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
- `jev_runner.py` — TypeSafe Jev classifier for the cloud-allowed partition, run as a
  Hermes `no_agent` cron script (see "Two cron jobs" below).
- `plugin.yaml` — version `1.3.0`.
- `test_status.py`, `test_diff.py`, `test_diagnostic.py`, `test_jev.py` — run directly
  (`python test_diff.py`, etc.); they use plain asserts, not test discovery.
- `CRON_PROMPT.md` — the model prompt for the scheduler.

## Two cron jobs, one privacy partition

Sessions are split by `is_cloud_allowed(profile)` in `__init__.py`: a profile is
cloud-allowed iff `<hermes root>/plugin-config/bot-acl/groups.yaml` loads as a
mapping, its `local` key is a list of strings, and the profile is not in it. The
file is read fresh on every call. **Any failure** (missing file, no PyYAML, parse
error, wrong shape) makes *no* profile cloud-allowed, so everything stays on the
local model. The Hermes managed Python (PyYAML) must run both jobs.

Note that profiles in no group (e.g. `school`, `deepseek`) count as cloud-allowed;
only the `local` group is protected.

| job | scope | classifier | entry |
|---|---|---|---|
| existing qwen agent job (`2d06e7e7352e`) | `local`: profiles NOT cloud-allowed | local model via `CRON_PROMPT.md` | `monitor_script.py` (gate) |
| new Jev job | `cloud`: cloud-allowed profiles | TypeSafe Jev, no LLM agent | `jev_runner.py` (`no_agent`) |

`monitor_script.py --scope local|cloud|all` selects the partition; with no
arguments it is `local`, so the live qwen job stops seeing cloud profiles as soon as
this version is deployed (its existing `session_status_gate.py` wrapper needs no
change). The two scopes are disjoint and together cover `all`. Out-of-scope
sessions are dropped before any evidence fetch.

### Jev job

- Wrapper `~/.hermes/scripts/session_status_jev.py`:

  ```python
  """Thin entry point; canonical implementation is managed in hermes-plugins."""
  import runpy
  runpy.run_path('/home/shi/ghq/github.com/shin902/hermes-plugins/session-title-status/jev_runner.py', run_name='__main__')
  ```
- Cron job: `no_agent`, script = that wrapper, same schedule as the qwen job. stdout
  is delivered verbatim (empty = silent), nonzero exit is an error alert.
- Needs `TYPESAFE_API_KEY` in the environment or in `<hermes root>/.env` (parsed,
  never sourced; never printed). Missing key with pending work: exit 1,
  `JEV_ERROR - - KeyMissing`.
- Per tick: `monitor_script.pending_work(scope='cloud')` (quiet window applies) ->
  `inspect` -> one Jev `choice` question (options `unresolved|outbox|review|running|done`
  = 🔴📤👀⏳✅; state = bounded messages + `truncated` + title without its emoji) ->
  `set_status` with that inspect's `evidence_hash`/`checkpoint_token`. `set_title`
  is never called. `is_cloud_allowed` is re-checked right before every network send.
- Confidence below `MIN_CONFIDENCE = 0.30` becomes 🔴. Calibrated on 40 recent
  `default`/`coding` sessions (dry-run): Jev confidence did not separate agreement
  with the qwen titles from disagreement (median 0.65 vs 0.60), so the threshold only
  drops near-uniform answers (~7-10%).
- Success output: `<profile>: N changed` per profile, nothing otherwise. A Jev
  transport/validation error or an `inspect`/`set_status` exception: no `set_status`
  for that session, other sessions continue, then stderr
  `JEV_ERROR <profile> <session> <class>` and exit 1. `skipped`, `retry` and
  `checkpoint conflict` are normal and picked up next tick.
- `python jev_runner.py --dry-run` prints one JSON line per session (profile, id,
  title, chosen emoji, jev_choice, confidence, probabilities) and writes nothing;
  `--recent N --profile P ...` (dry-run only) classifies the N newest eligible
  sessions regardless of checkpoint, for calibration.
- Sessions sent to Jev: title-less-emoji + latest <=6 messages (<=12000 chars).
  Run `--dry-run` once with the production environment before enabling the cron.

## Parent install: edit the EXISTING cron entry

The parent must edit the existing automation/cron entry for the qwen job; do **not**
create a second *agent* entry. The Jev job above is the only additional entry (`no_agent`).

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

Sessions active within the last 120 seconds (`QUIET_SECONDS`) are deferred: the
gate skips them entirely — no evidence fetch, no classification — so a
mid-conversation session is not classified and then rejected with
`retry: evidence changed` after the model has already spent its run. The
checkpoint is left untouched, so the session becomes pending again on the first
tick after it has been quiet for 120 seconds. Missing or unparseable
`last_active` values are never deferred.

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
