# bot-acl

Local Bot Mode sending policy plus automatic-roster filtering, implemented with the standard Hermes plugin API. No private Hermes roster/router functions or core patches are used.

## Sending policy

`pre_tool_call` rejects `message_agent` unless the sender and target belong to the same single group. Targets use canonical lowercase profile IDs (optionally prefixed with `@`); the reserved `hermes` handle identifies `default`. Arbitrary friendly-name aliases are rejected.

Missing/deleted local targets, self-targets, external-qualified targets, peer-name collisions, and unreadable/malformed configuration are denied. The existence check requires `profile.yaml` or `config.yaml` and checks `profiles/.deleted/<id>`; this conservative file-format dependency replaces private-function dependencies. Older config-only profiles remain supported.

## Automatic roster visibility

`llm_request` filters the automatic `## Messaging other agents` roster in system/developer context and Responses `instructions` before provider execution. Only permitted local teammates are listed; remote/peer roster addenda are removed. Allowed teammates retain their role descriptions, the default profile is shown as `@default`, and delivery/privacy instructions, capability stamps and unrelated surrounding instructions are preserved.

Chat Completions `messages`, Responses `input`/`instructions`, and text-block content are supported. User, assistant and tool history, tool schemas, stored prompts, profile configuration and the Desktop's human-facing Bots list are not modified.

The permitted roster is frozen per profile/session/group configuration (bounded in-process cache) to avoid per-turn prefix drift. Reload the plugin or use a new session to refresh it after changing peers/profile availability. Sending checks remain live even when the displayed roster is frozen.

## Consultations and asynchronous bot tasks (0.6.0)

The `bot_consult` toolset adds `list_bot_peers`, `ask_bot`, and `bot_task` to ordinary sessions. Enable it with `hermes -p <profile> tools enable bot_consult`; use a new session after enabling/reloading. Each handler rechecks the live ACL; the `pre_tool_call` guard also covers `ask_bot` and `bot_task` spawn targets.

`ask_bot` is the synchronous one-shot consultation tool. It accepts `target`, `message`, and optional `mode` (`new_session` by default, or `resume` with an exact `session_id`). The reply is returned in the tool result. There is no `bot_chat` mode and no canonical Bot Chat lookup/continue behavior. Calls have no caller-selectable timeout: the runtime always uses `--run-budget 0` and `--max-turns 250`, with no wall-clock deadline. Supplying `timeout_seconds` is rejected explicitly, including `0`, rather than ignored. Nested consultations are disabled. No daemons, resident workers, queues, watchers, or callback loops are installed.

`bot_task` actions are `spawn`, `list`, `status`, `wait`, `steer`, and `stop`. `spawn` requires `target` and `message` and accepts:

- `mode=new_session` (default): a fresh child session in the recipient profile.
- `mode=resume` with an exact `session_id`: resume one stored session that already exists in the target profile. Titles, `latest`, aliases, and caller-supplied IDs from another profile are rejected before a child starts.

There is no `bot_chat` mode and no canonical Bot Chat creation or continuation. `bot_task` has no `timeout_seconds` field and no wall-clock timer; supplying it is rejected explicitly, including `0`. `status`, `wait`, `stop`, and `steer` require `task_id`; `list` takes no extra fields. `status` and `wait` retrieve the reply; `steer` reports `accepted_unconfirmed` because backend queueing is ambiguous; `stop` kills and reaps the dedicated child process group. There is no automatic retry, mode switch, or resend.

`ask_bot` transport uses only the public `hermes -p <target> chat --query-file - --format stream-json` surface with `--run-budget 0` and `--max-turns 250`. `bot_task` transport uses the installed official TUI stdio gateway (`tui_gateway.entry`) and its public JSON-RPC methods: `session.create`, `session.resume`, `prompt.submit`, `session.steer`, and `session.activate`. Target session ownership uses the same process-shared lease registry as the TUI, so a second writer is refused rather than silently starting a competing turn. Child tasks are started asynchronously and are parent-owned; a parent can list only its own tasks, active-task limits are preserved, and the startup thread retains caller context. Nested child tasks are disabled. Transport failure, cancellation, and explicit partial results are never reported as completed replies, and finality is explicitly unverified because `message.complete` can precede backend post-turn follow-ups.

A standard `register_system_prompt_section` for each tool adds fixed guidance to new session prompts. Peer discovery is a tool, not a global roster dump. No CLI impersonation or private Hermes DM helpers, process registries, session-injection internals, or core patches are used.

## Consultation and task hardening (0.6.0)

- A request for `resume` never creates a new session. The exact stored ID is checked read-only in the target profile before launch; titles, `latest`, aliases, and IDs from another profile are rejected before a child starts.
- The verified parent `session_id` comes only from the tool runtime keyword arguments and owns the task record. `spawn.session_id` is accepted only for `mode=resume` and is the target-owned stored session.
- The TUI ownership refusal (`SESSION_NOT_OWNED`, code 4090, or a busy session) becomes `target_busy` with no resend. Other startup failures become `failed_unknown`; synchronous `ask_bot` keeps its typed `target_busy` refusal and audit trail. No timeout result is producible through either tool interface because neither exposes a timer.
- Neither tool accepts `timeout_seconds`; the key is rejected explicitly even when its value is `0`. `bot_task` creates no timer and no deadline, while `stop` still terminates and reaps the child.
- Synchronous `ask_bot` retains profile-local, atomic 0600 minimal request audits without question/reply bodies, bounded stream parsing, init/result/stderr session-ID evidence, process-group cleanup, and no automatic resend.
- Retained task state is process-local and bounded. Stops are idempotent, cleanup failures do not replace an interruption, and `atexit` closes owned children.

## Limits

This is context hygiene, not secrecy or sandbox isolation. Earlier history can still contain bot names. The recognized heading/text format can change in Hermes releases, and host middleware failures are fail-open; retain the independent sending ACL. Unknown roster layout under the recognized heading is replaced conservatively and logged. A renamed heading or unsupported payload layout needs an update.

The TUI stdio JSON-RPC surface is documented and shipped by the installed Hermes release, but this plugin has no separate stable third-party compatibility guarantee for that entrypoint. Missing or changed methods fail closed without a fallback transport. `message.complete` does not certify that backend-owned pending follow-ups have drained, so status reports `finality_verified=false`; a completed task may return to running while follow-up work proceeds.


## Verification

Run with the Hermes runtime:

```sh
hermes --run-module unittest discover -s "$PWD/bot-acl" -p 'test_*.py'
hermes plugins validate "$PWD/bot-acl"
hermes plugins doctor "$PWD/bot-acl" --ci
```

Keep each profile's existing `DOMAINS` unchanged when deploying the common `policy.py`/`roster.py` implementation. Some profiles share the entire plugin while others share only those implementation files.
