# bot-acl

Local Bot Mode sending policy plus automatic-roster filtering, implemented with the standard Hermes plugin API. No private Hermes roster/router functions or core patches are used.

## Sending policy

`pre_tool_call` rejects `message_agent` unless the sender and target belong to the same single group. Targets use canonical lowercase profile IDs (optionally prefixed with `@`); the reserved `hermes` handle identifies `default`. Arbitrary friendly-name aliases are rejected.

Missing/deleted local targets, self-targets, external-qualified targets, peer-name collisions, and unreadable/malformed configuration are denied. The existence check requires `profile.yaml` or `config.yaml` and checks `profiles/.deleted/<id>`; this conservative file-format dependency replaces private-function dependencies. Older config-only profiles remain supported.

## Automatic roster visibility

`llm_request` filters the automatic `## Messaging other agents` roster in system/developer context and Responses `instructions` before provider execution. Only permitted local teammates are listed; remote/peer roster addenda are removed. Allowed teammates retain their role descriptions, the default profile is shown as `@default`, and delivery/privacy instructions, capability stamps and unrelated surrounding instructions are preserved.

Chat Completions `messages`, Responses `input`/`instructions`, and text-block content are supported. User, assistant and tool history, tool schemas, stored prompts, profile configuration and the Desktop's human-facing Bots list are not modified.

The permitted roster is frozen per profile/session/group configuration (bounded in-process cache) to avoid per-turn prefix drift. Reload the plugin or use a new session to refresh it after changing peers/profile availability. Sending checks remain live even when the displayed roster is frozen.

## Consultations and asynchronous bot tasks (0.7.0)

The `bot_consult` toolset adds `list_bot_peers`, `ask_bot`, and `bot_task` to ordinary sessions. Enable it with `hermes -p <profile> tools enable bot_consult`; use a new session after enabling/reloading. Each handler rechecks the live ACL; the `pre_tool_call` guard also covers `ask_bot` and `bot_task` spawn targets.

`ask_bot` is the synchronous one-shot consultation tool. It accepts `target`, `message`, and optional `mode` (`new_session` by default, or `resume` with an exact `session_id`). The reply is returned in the tool result. There is no `bot_chat` mode and no canonical Bot Chat lookup/continue behavior. Calls have no caller-selectable timeout: the runtime always uses `--run-budget 0` and `--max-turns 250`, with no wall-clock deadline. Supplying `timeout_seconds` is rejected explicitly, including `0`, rather than ignored. Nested consultations are disabled. No daemons, resident workers, queues, watchers, or callback loops are installed.

`bot_task` actions are `spawn`, `list`, `status`, `wait`, `steer`, and `stop`. `spawn` requires `target` and `message` and accepts:

- `mode=new_session` (default): a fresh child session in the recipient profile.
- `mode=resume` with an exact `session_id`: resume one stored session that already exists in the target profile. Titles, `latest`, aliases, and prefixes are rejected before a child starts; the exact ID is checked read-only in the selected profile before any CLI turn is launched.

There is no `bot_chat` mode and no canonical Bot Chat creation or continuation. `bot_task` has no `timeout_seconds` field and no wall-clock timer; supplying it is rejected explicitly, including `0`. `status`, `wait`, `stop`, and `steer` require `task_id`; `list` takes no extra fields. `status` and `wait` retrieve the current turn's reply or partial evidence. `stop` terminates the dedicated CLI process group. There is no automatic retry, mode switch, or resend.

`steer` interrupts the active public CLI turn with `SIGTERM`, waits for the predecessor process group to exit, and then resumes the same returned `session_id` in the same target profile with only the new guidance. The original prompt is never resent. A completed turn cannot be steered: `steer` returns `not_applied_completed` and sends nothing. If shutdown requires a bounded `SIGKILL`, persistence is uncertain and automatic resume is blocked (`blocked_forced_kill`). If the interrupted turn returned no `session_id`, resume is also blocked (`blocked_no_session_id`). Stop sets a race flag before waiting for an in-flight steer, so a later steer cannot start a new writer; multiple steers are serialized by the task lock.

### Transport

Both tools use only the public CLI surface:

```text
hermes -p <target> chat --in <home> --query-file - --format stream-json \
  --source cli --run-budget 0 --max-turns 250 [--resume=<exact-id>]
```

Each `bot_task` turn is one such subprocess. There is no TUI stdio JSON-RPC gateway, HTTP listener, shared gateway mutation, or arbitrary RPC passthrough. Target profile isolation uses `-p <target>`, `HERMES_HOME`, and removal of inherited TUI/profile/session environment variables. Child tasks are started asynchronously and are parent-owned; a parent can list only its own tasks, active-task limits are preserved, and the startup thread retains caller context. Nested child tasks are disabled. Transport failure, cancellation, and explicit partial results are never reported as completed replies.

### Exact-ID resume blocker

The public CLI does not expose a safe exact-ID existence check:

- `chat --resume` accepts an exact ID, a title, or `latest`, and may follow a compression tip.
- `hermes sessions export --session-id` accepts unique prefixes and materializes transcript data.
- `hermes sessions list` is capped and has no exact-ID filter.

The plugin therefore retains a read-only, parameterized `SELECT 1 FROM sessions WHERE id = ?` probe in the selected profile before resume. This is a documented internal storage dependency and the remaining upstream seam: a public CLI command that validates an exact stored session ID without accepting titles, prefixes, `latest`, or foreign profiles is still missing. The probe never widens accepted input; if the store is absent or unreadable, resume fails closed.

## Consultation and task hardening (0.7.0)

- A request for `resume` never creates a new session. Titles, `latest`, aliases, prefixes, and malformed IDs are rejected before launch; the exact stored ID is checked read-only in the target profile.
- The verified parent `session_id` comes only from the tool runtime keyword arguments and owns the task record. `spawn.session_id` is accepted only for `mode=resume` and is the target-owned stored session.
- The public CLI's typed ownership refusal (`hermes-refusal-reason: SESSION_NOT_OWNED`) becomes `target_busy` with no resend. Other startup failures become `failed_unknown`; synchronous `ask_bot` keeps its typed `target_busy` refusal and audit trail.
- Neither tool accepts `timeout_seconds`; the key is rejected explicitly even when its value is `0`. `bot_task` creates no timer and no deadline, while `stop` still terminates and reaps the process group.
- Synchronous `ask_bot` retains profile-local, atomic 0600 minimal request audits without question/reply bodies, bounded stream parsing, init/result/stderr session-ID evidence, process-group cleanup, and no automatic resend.
- Retained task state is process-local and bounded. Stops are idempotent, cleanup failures do not replace an interruption, and `atexit` closes owned turns.

## Limits

This is context hygiene, not secrecy or sandbox isolation. Earlier history can still contain bot names. The recognized heading/text format can change in Hermes releases, and host middleware failures are fail-open; retain the independent sending ACL. Unknown roster layout under the recognized heading is replaced conservatively and logged. A renamed heading or unsupported payload layout needs an update.

The public `hermes chat --format stream-json` surface is shipped by the installed Hermes release, but this plugin has no separate stable third-party compatibility guarantee. Missing or changed flags fail closed without a fallback transport. A completed CLI process has exited, so its final result is final for that turn; interrupted and forced-kill outcomes are explicitly partial/unknown. The SQLite exact-ID probe depends on the installed `sessions.id` schema and is the documented blocker above. A live model-driven steer test is not performed; deterministic fake-CLI subprocess tests cover the protocol, and public `--help`/`sessions list` behavior is exercised in an isolated home.

## Verification

Run with the Hermes runtime:

```sh
hermes --run-module unittest discover -s "$PWD/bot-acl" -p 'test_*.py'
hermes plugins validate "$PWD/bot-acl"
hermes plugins doctor "$PWD/bot-acl" --ci
```

Keep each profile's existing `DOMAINS` unchanged when deploying the common `policy.py`/`roster.py` implementation. Some profiles share the entire plugin while others share only those implementation files.
