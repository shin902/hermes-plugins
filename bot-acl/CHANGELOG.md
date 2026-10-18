# Changelog

## 0.5.0

- Preserve valid stream-json init session IDs during execution and independently of stdout tail truncation; terminal result IDs take precedence, stderr remains the final fallback.
- Add profile-local, atomic 0600 minimal request audits without question/reply bodies, including interruption and cleanup state.
- Default timeout to 0 (no wall-clock limit) in schema, validation, CLI argv and parent transport; preserve finite 30–300 second compatibility.
- Replace the 100-iteration CLI override with 250 agent iterations.
- Reuse installed receiver-session ownership fencing; do not add caller/target-wide locks, arbitrary-session access, private API imports or core patches. Document installed CLI unexpected-exception fail-open limitation.
- Add model-free fixtures for parsing, large stdout, finite/unlimited timeouts, real cancellation cleanup, private-body-free audit records, registration/schema and process-shared native ownership.
