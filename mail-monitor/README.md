# Outlook mail monitor

Native Hermes plugin, no core edits, internal function replacements, background daemon, or ctx.llm.

## Pipeline

- Native script-only cron runs `hermes -p mail mail-collect` every minute. MSAL silent token refresh and Graph pagination/body retrieval are deterministic. Authentication failures never start an interactive login in cron.
- A separate native monitor cron runs `hermes -p mail mail-monitor`, supplies exactly one queued mail, and starts the normal agent only when output changes. Pending attempts carry a new generation so a failed agent cannot lose work to the monitor's pre-run hash snapshot. Empty output is the stable `NO_PENDING_MAIL` marker.
- The agent writes a Japanese summary and calls `mail_publish(batch_id, summary)`. The tool is scoped to the configured mail cron execution. It publishes to the fixed channel through public `hermes send`, records a message ID, and only then marks Graph `isRead=true` and reads that field back.
- A successful send with failed Graph ACK is not re-sent. Collection retries only ACK. Sending/unknown windows remain held for operator reconciliation, not automatic duplicate delivery.
- All native final-answer and failure delivery targets are `local`. Collector stdout is empty. No failure/recovery/outage messages are sent to Discord.

## Paths

Installed profile: `/home/shi/.hermes/profiles/mail`.
Settings: `plugins/mail-monitor/settings.json` (nonsecret Microsoft app configuration and fixed target).
Separate copied token cache: `credentials/graph-cache.json` (0600). Original cache is not updated by this plugin.
State/health: `cron/mail-monitor/` (contains private mail data).
Cron scripts: `scripts/mail_collect.py`, `scripts/mail_monitor.py`.
Node dependencies are separately installed in the plugin directory and locked in package-lock.json; install with `npm ci --ignore-scripts` when restoring.

## Scope and limits

- Publishes into sender-address threads or GitHub repository + PR/issue-number threads under the original channel `1511360574607593555`, matching legacy mailRouteKey. List-ID is accepted for GitHub routing only from notifications@github.com. Commit-SHA PR-run failure notifications resolve through GitHub commit/pulls lookup; ambiguous results fall back to the sender thread.
- `cron/mail-monitor/threads.json` retains legacy route keys and exact Discord thread IDs imported from a copied runtime.sqlite. Existing threads are reused; archived threads are unarchived before sending; deleted threads are recreated. New thread creation is durably fenced before POST so unknown results cannot blindly create duplicates. Discord thread operations use the public Discord REST API and the existing default-owned transport token; message sending uses public `hermes send`.
- Optional GitHub routing credential is independently copied into `credentials/github-route-token` (0600), never exposed to model tools.
- Parent-channel messages published before thread support are not deleted or reposted automatically.
- Current unread inbox mail is queued, including backlog. No initial read-all or discard baseline.
- Read mail, sending replies, deleting mail, and attachments are not exposed as tools.
- Text bodies longer than 24000 characters are explicitly truncated. Summary must fit one Discord text message.
- Discord receipt IDs are not exactly-once guarantees. Ambiguous sends remain retained/held.
- `mail_publish` validates ownership and arguments in code. Tool calling by the model is still probabilistic; pending generations permit a later attempt if it omits publication.
- Mail profile retains its existing local qwen3.8-flash-next model. Default profile owns the shared Discord transport; no bot token is copied into mail.

## Verification

`python test_runtime.py` runs isolated synthetic fixtures, no real mail writes or Discord posts.
`hermes -p mail plugins doctor /home/shi/.hermes/profiles/mail/plugins/mail-monitor --ci` validates real plugin discovery.
Inspect `hermes -p mail cron runs <job>` and durable mail receipts separately; a scheduler success is not itself proof of publication.
Live acceptance requires an actual Discord message readback and Graph isRead readback, both performed during deployment.
