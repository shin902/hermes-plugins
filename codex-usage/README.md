# Codex usage automation

A standard Hermes Python plugin, deployed only to the coding profile. Monitoring uses the public Codex app-server `account/rateLimits/read` method and does not call a model. Five-hour and weekly windows are classified by `windowDurationMins` (300/10080), not slot position. The existing three five-minute `--script --no-agent` cron jobs and profile-local state/queue remain unchanged.

- `codex_usage_monitor.py`: read and record usage; emit only a changed snapshot.
- `codex_usage_prime.py`: prime after an observed five-hour deadline expires or rolls into a new, still-unused window; uncertain outcomes are fenced from retry. A future window with any recorded usage is treated as already active.
- `codex_usage_task.py`: after 150 minutes in a live 300-minute window and with at least 80% remaining, run queued tasks one at a time. Weekly allowance is not reserved.

Queued runs are persistent Codex sessions (not `--ephemeral`). Their exact UUID from the `thread.started` JSON event is saved as `taskSessionId` and in task history. A claimed task is durably stored before dequeue; failure keeps the prompt and fences automatic relaunch. Queue edits use the same `actions.lock` as the cron worker.

## CLI

The standalone script and native Hermes command share the same argparse CLI:

```sh
python3 /path/to/codex-usage/cli.py status             # cached snapshot only
python3 /path/to/codex-usage/cli.py status --fresh      # fetch and cache
python3 /path/to/codex-usage/cli.py tasks list
python3 /path/to/codex-usage/cli.py tasks add --id item-1 --cwd /absolute/work --prompt 'Do the task'
python3 /path/to/codex-usage/cli.py tasks remove item-1
python3 /path/to/codex-usage/cli.py resume 12345678-1234-4234-8234-123456789abc --cwd /absolute/work --sandbox workspace-write --prompt 'Continue the same task'
```

After enabling the plugin, use the same commands under `hermes codex-usage ...`. Resume requires one exact UUID; malformed IDs are rejected locally, and a failed resume never creates a replacement session. Resume runs from the explicit/current `--cwd`. Its default is read-only; request `--sandbox workspace-write` when that session needs writes. Initial queued sessions use `workspace-write` with their task's explicit cwd.

Queue data is outside Git at `$HERMES_HOME/plugin-data/codex-usage/tasks.json`; monitor/task/prime state remains at `$HERMES_HOME/plugin-data/codex-usage/{state,actions}.json`. Default task model is `gpt-6.1-sol`; optional `model` and `reasoning_effort` are accepted. Discord delivery is currently unavailable in this profile, so cron output is local.

Run `python3 test_monitor.py`, `python3 test_actions.py`, and `python3 test_cli.py` from this directory. Hermes integration checks: `hermes plugins validate ./codex-usage` and `hermes -p coding plugins doctor codex-usage --ci`.
