# bot-acl

Local Bot Mode sending policy plus automatic-roster filtering, implemented with the standard Hermes plugin API. No private Hermes roster/router functions or core patches are used.

## Sending policy

`pre_tool_call` rejects `message_agent` unless the sender and target belong to the same single group. Targets use canonical lowercase profile IDs (optionally prefixed with `@`); the reserved `hermes` handle identifies `default`. Arbitrary friendly-name aliases are rejected.

Missing/deleted local targets, self-targets, external-qualified targets, peer-name collisions, and unreadable/malformed configuration are denied. The existence check requires `profile.yaml` or `config.yaml` and checks `profiles/.deleted/<id>`; this conservative file-format dependency replaces private-function dependencies. Older config-only profiles remain supported.

## Automatic roster visibility

`llm_request` filters the automatic `## Messaging other agents` roster in system/developer context and Responses `instructions` before provider execution. Only permitted local teammates are listed; remote/peer roster addenda are removed. Allowed teammates retain their role descriptions, the default profile is shown as `@default`, and delivery/privacy instructions, capability stamps and unrelated surrounding instructions are preserved.

Chat Completions `messages`, Responses `input`/`instructions`, and text-block content are supported. User, assistant and tool history, tool schemas, stored prompts, profile configuration and the Desktop's human-facing Bots list are not modified.

The permitted roster is frozen per profile/session/group configuration (bounded in-process cache) to avoid per-turn prefix drift. Reload the plugin or use a new session to refresh it after changing peers/profile availability. Sending checks remain live even when the displayed roster is frozen.

## On-demand consultations (0.3.0)

The `bot_consult` toolset adds `list_bot_peers` and `ask_bot` to ordinary sessions. Enable it with `hermes -p <profile> tools enable bot_consult`; use a new session after enabling/reloading. Each call checks the existing ACL inside the handler, and the `pre_tool_call` guard also covers `ask_bot`.

`ask_bot` accepts `target`, `message`, optional `mode` (`new_session` by default, or `bot_chat`), and optional `timeout_seconds` (30–300; default 180). The agent selects the mode; there is no automatic retry or mode switch. `new_session` uses a fresh session in the recipient profile, without the canonical Bot Chat transcript. `bot_chat` resumes the existing `Bot Chat` by the public CLI title lookup; it does not create a look-alike chat when none exists. An exact CLI ownership-refusal marker returns `target_busy` plus guidance to use `new_session` if Bot Chat history is unnecessary.

Transport uses only public `hermes -p <target> chat --query-file - --format stream-json` with a run budget and turn cap. Payload is passed through stdin, and the recipient's terminal result is returned synchronously. No daemons, resident workers, queues, watchers, or callback loops are installed. Child consultation tools are disabled to prevent nested `ask_bot` loops. On timeout or interruption the owned, still-running process group is killed and the leader reaped. Timeout means outcome unknown, not guaranteed non-delivery; do not auto-resend. Recipient prompts explicitly prohibit background work and ask for a final reply rather than a separate message to the sender; this is not a sandbox for arbitrary recipient tools.

A standard `register_system_prompt_section` adds fixed usage guidance to new session prompts. Peer discovery is a tool, not a global roster dump. Public CLI flags and JSON result format remain compatibility dependencies, but no private Hermes DM helpers, process registries, session-injection internals, or core patches are used.

## Limits

This is context hygiene, not secrecy or sandbox isolation. Earlier history can still contain bot names. The recognized heading/text format can change in Hermes releases, and host middleware failures are fail-open; retain the independent sending ACL. Unknown roster layout under the recognized heading is replaced conservatively and logged. A renamed heading or unsupported payload layout needs an update.

## Verification

Run with the Hermes runtime:

```sh
hermes --run-module unittest discover -s /home/shi/.hermes/plugins/bot-acl -p 'test_*.py'
hermes plugins doctor /home/shi/.hermes/plugins/bot-acl
```

Keep each profile's existing `DOMAINS` unchanged when deploying the common `policy.py`/`roster.py` implementation. Some profiles share the entire plugin while others share only those implementation files.
