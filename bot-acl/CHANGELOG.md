# Changelog

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
