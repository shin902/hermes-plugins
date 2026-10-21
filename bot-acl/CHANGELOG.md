# Changelog

## 0.7.0

- Replace the `bot_task` TUI stdio JSON-RPC backend with one public `hermes -p <target> chat --query-file - --format stream-json` subprocess per turn, always with `--run-budget 0` and `--max-turns 250`.
- Keep `spawn`, `list`, `status`, `wait`, `steer`, and `stop`, parent ownership, same-group ACL, nested-call denial, profile isolation, active-task limits, partial-result reporting, and no automatic resend.
- Change `steer` to interrupt the active CLI generation or tool execution with `SIGTERM`, wait for the predecessor process group to exit, then resume the same returned session ID in the same target profile with only the new guidance. The original prompt is never resent.
- Serialize steer/completion/multiple-steer, make stop win races, and block automatic resume after a forced `SIGKILL`, uncertain persistence, or a missing session ID.
- Report completed turns truthfully after a late stop; only an actually interrupted turn becomes `stopped`/`stopped_unknown`.
- Prefer a terminal result's `session_id` over the turn's init ID so a compression continuation resumes the returned session; retain the init and exact requested-resume fallbacks.
- Keep an interrupting steer handoff active after its predecessor turn settles so concurrent spawns cannot exceed the owner/process limits or evict the handoff task from the process-local task map.
- Preserve actionable task-level errors (missing session ID, revoked ACL, replacement launch failure) instead of masking them with the interrupted predecessor turn's error.
- When `stop` finds no turn, only mark a pending start as stopped; preserve a settled startup failure and report `already_finished`.
- Remove TUI protocol and private Hermes runtime imports from the plugin and tests. Public `--help`/`sessions list` behavior and deterministic fake-CLI subprocesses are exercised instead.
- Document the remaining exact-ID blocker: the public CLI accepts titles, `latest`, or prefixes, so a read-only parameterized SQLite exact-ID probe is retained rather than weakening resume validation.

## 0.6.0

- Keep the synchronous `ask_bot` tool, its registration, auditing, prompt and public CLI transport; also keep `bot_task`.
- Remove the `bot_chat` mode from both consultation tools. There is no canonical Bot Chat lookup, creation, or continue behavior.
- Keep `new_session` and exact target-owned `resume` in both tools. Resume rejects titles, `latest`, and aliases lexically before launch. `bot_task` delegates existence and ownership to the target-scoped `session.resume` RPC instead of probing the private `state.db`/`sessions` table.
- Remove caller `timeout_seconds` from both tool schemas and handlers. Runtime is always unlimited (`--run-budget 0` for `ask_bot`, no wall-clock timer for `bot_task`); supplied keys are rejected explicitly, including `0`.
- Add `bot_task` actions `spawn`, `list`, `status`, `wait`, `steer`, and `stop`, with parent ownership, same-group ACL, nested-call blocking, active-task limits, session-ownership/busy refusal, no automatic resend, stop cleanup, and explicitly unverified finality.
- Fix the `bot_task` schema description: `wait` requires `task_id`; only `list` takes no extra fields.
- Route target session resumption through the installed TUI stdio protocol (`session.resume`) and the process-shared ownership lease while retaining the public synchronous CLI regression coverage. No private session-store access is used by `bot_task`.

## 0.5.0

- Preserve valid stream-json init session IDs during execution and independently of stdout tail truncation; terminal result IDs take precedence, stderr remains the final fallback.
- Add profile-local, atomic 0600 minimal request audits without question/reply bodies, including interruption and cleanup state.
- Default timeout to 0 (no wall-clock limit) in schema, validation, CLI argv and parent transport; preserve finite 30–300 second compatibility.
- Replace the 100-iteration CLI override with 250 agent iterations.
- Reuse installed receiver-session ownership fencing; do not add caller/target-wide locks, arbitrary-session access, private API imports or core patches. Document installed CLI unexpected-exception fail-open limitation.
- Add model-free fixtures for parsing, large stdout, finite/unlimited timeouts, real cancellation cleanup, private-body-free audit records, registration/schema and process-shared native ownership.
