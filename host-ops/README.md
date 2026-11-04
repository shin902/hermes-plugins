# host-ops

Hermes plugin exposing one `host_ops(action, params)` tool. It uses only the public `register(ctx)` / `ctx.register_tool(...)` API. Every operation is selected from an allowlist, launched with fixed executable paths and argument arrays (`shell=False`), bounded by output and timeout limits, and run with a deterministic environment that excludes inherited `LD_PRELOAD`, `PYTHONPATH`, and ambient credentials. Its working directory is `/`; profile-scoped actions default to the calling profile.

## Actions

Every `host_ops` call, including reads, requests human approval through the documented
`pre_tool_call` hook. Each call uses a fresh rule key so a previous session/always
approval cannot authorize another call. `approvals.mode: smart` is supported: plugin
approval requests go directly to the human gate, not the auxiliary LLM. Hermes's
`off`/YOLO and unattended approval policies can bypass the standard gate.
This hook does not escalate `terminal` (including sandbox Git) or `host_github`.
The plugin does not supply host-authenticated Git fetch/push.

- `host_diagnostics`: bounded CPU/memory information, top 20 process rows, TCP listening ports and GPU summaries (`nvidia-smi` or `/opt/rocm/bin/rocminfo` when present).
- `health_check`: fixed GET-only localhost probes: `logbook` (`127.0.0.1:8788/health`), `metro` (`127.0.0.1:8081/status`), `preview` (`127.0.0.1:8082/`), and `strata` (`127.0.0.1:8080/health`). Redirects are rejected, the body is capped at 4 KiB, and timeout is 3 seconds. No caller URL, headers, method, or path is accepted.
- `service_status`, `service_logs`, `service_start`, `service_stop`, `service_restart`, `daemon_reload`: fixed `/usr/bin/systemctl --user` and `/usr/bin/journalctl --user`, restricted to `hermes-gateway.service`, `hermes-logbook-capture.service`, and `llama-swap.service`. Mutations require `confirm: true` (input validation only, not a human approval prompt); `config_set` also requires it. Service actions read active state back and stop requires the exact `inactive` state. Uncertain/nonzero changes report `verified: false`, `retry_safe: false`; a restart's active state is not an application health check. `daemon_reload` reports command execution separately and remains unverified because it has no independent state readback.
- `cron_list`, `cron_status`, `cron_runs`, `cron_pause`, `cron_resume`, `cron_run`: fixed Hermes launcher `/home/shi/.hermes/hermes-agent/.hermes/bin/hermes`; cron actions accept an optional allowlisted `profile` and default to `coding`. The selected profile directory must exist, the same profile is used for CLI invocation and registry readback, and configuration remains coding-only. Only profiles coding, default, logbook, mail, finance, computer-use, meta-bot, and local are accepted by Docker inspection. Cron mutations require `confirm: true`. Pause/resume verify the exact job ID and durable `enabled` value in the selected profile registry. Run verification follows the installed CLI result (`Ran now`, background dispatch, or next scheduler tick), rather than treating every zero exit as queued.
- `config_get`, `config_set`: only `terminal.backend` (local/docker), `terminal.docker_network`, `terminal.docker_mount_cwd_to_workspace`, `terminal.container_persistent`, `terminal.timeout` (1–600), `model.supports_vision`, and `agent.image_input_mode` (auto/native/text, matching the installed Hermes routing implementation). Writes are made only via Hermes CLI, then the exact setting is read back and its type and value compared. Arbitrary config fields, secrets, plugin installation, shell hooks, and other config operations are intentionally omitted. No mount settings were changed while building or verifying this plugin.
- `docker_list`, `docker_inspect`: read-only list/inspect scoped to containers carrying the exact `hermes-agent=1` label; inspection also requires an exact allowlisted `hermes-profile` label. Output omits environment variables and reports only name/image/status, `hermes-*` labels, mount destinations/type/read-write flag. No `docker exec`, arbitrary Docker commands, socket forwarding, or container removal is exposed: those would provide a host-shell escape. Container remediation needs a separate deliberate administrator action.

## Scope and limits

This plugin supplies host diagnostics and narrow service controls for sandboxed coding terminal work. Git, issues and PRs belong to the separate `host-github` plugin, whose direct host Git explicitly trusts checkout configuration and hooks. This plugin does not launch GPU benchmarks or inference from caller-selected scripts. It is not a complete migration of every host operation.

The audit log stores only UTC timestamp, validated action name, and success boolean in `audit/calls.jsonl` with private permissions; malformed parameters and output are excluded. `confirm: true` is a validation gate and does not itself constitute interactive human approval.

## Install/activate

Install it in any profile's plugin directory and enable it there:

```sh
hermes plugins enable host-ops
```

No Hermes configuration, enabled plugin list, live service, GitHub repository, or device was changed during verification.

## Verify

```sh
python -m unittest discover -s <this-directory>/tests -v
hermes plugins doctor <this-directory> --ci
```

Tests use local child processes and mocks; they do not mutate host services, cron registry, config, Docker, network services, GitHub, or devices. The test suite passed in the coding sandbox. The sandbox doctor invocation could not complete because Hermes attempted writes to read-only install locks (`.prepare.lock` and `.install.lock`); this is a sandbox limitation, not a plugin defect.

Parent-reported host verification: plugin doctor passes. Parent-reported live read-only host smoke results: `hermes-logbook-capture.service` active; `cron_status` exit 0; `config_get terminal.backend` returned `local`; Docker listed two managed containers; logbook health returned HTTP 200. These are host observations supplied by the parent run, distinct from sandbox fixtures; no live host operation was performed during this change.
