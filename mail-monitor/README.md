# Outlook mail monitor

Native Hermes plugin, no core edits, internal function replacements, background daemon, or ctx.llm.

## Active deployment: private local pipeline (2026-10-27)

- `discord_notifications: false` makes `runtime.publish` atomically save the
  existing `delivered[*].record.{mail,summary}` contract without resolving a
  thread or calling any Discord transport. It does not discard summaries.
- Exact existing summary job `10711e0b54ab` now runs
  `scripts/mail_process_local.py` in **no-agent** mode; its old monitor source
  is cleared. Collection job `5f422d97eaf3` remains active and unchanged.
  Both final/failure delivery targets are local. No extra scheduler was added.
- `mail-process-local` consumes the same pending/current queue. It summarizes
  through the configured numeric LAN endpoint/model only (qwen3.8-flash-next).
  No Hermes agent, cloud provider, tools, profile history or fallback routing
  sees original mail. HTTP proxies and redirects are disabled; public endpoints
  and DNS hostnames are rejected. Endpoint failure retains the ready batch.
- `personal-dashboard/dashboard/school_auto.py` imports school originals from
  delivered records using the attachment parser first. Low/medium confidence,
  overlapping role cues, invalid dates/times/weekday, reversed ranges, explicit
  year disagreement, and uncovered date tokens trigger local validation.
  Unlike `--llm-cmd`, fallback can recover missed date spans (including M/D),
  but only exact original spans/evidence reparsed deterministically can supply
  dates. Model dates must match both normalized endpoints; invented evidence,
  unsupported month-only ranges and unresolved markers are rejected.
- Unresolved originals appear in the school screen as **要確認**, with no
  guessed recommended date. Detailed candidate analysis stays in the private
  `school_mail_imports` table. Oversized display bodies are flagged for review;
  full originals remain in the mail archive. URLs are not interpreted as dates.
- Notice blocks and their source checkpoint commit together. Restart/repeat
  skips checkpointed originals without inference, never overwrites user edits,
  and never recreates user-deleted notices. Changed source hashes are marked
  `source_changed`, not applied over edits. Inference/storage failures leave no
  successful checkpoint, and retry on the next existing cron tick. Ambiguous
  old Discord sending/unknown batches remain held, never blindly resent.
- Each invocation processes up to five summaries and ten previously unseen
  school mails. Private processing returns counts/status only in
  `cron/mail-monitor/pipeline-health.json`, including durable cumulative counts.
  Collection, summary, school import and retry never acknowledge Outlook.

### Verification entrypoints

- `python3 test_local_pipeline.py`: synthetic persistence, no transport/Graph,
  restart/retry/unknown fences, reader contract and rejection of cloud endpoints.
- `python3 check_local_inference.py`: real LAN inference with temporary synthetic
  mail/DB; verifies a newly saved summary, rescued M/D date, and idle repeat with
  zero new inference. Never processes real mailbox state.
- Dashboard `python3 -m unittest test_school_auto -v`: grounding, missed dates,
  review states, Graph CRLF, manual edit/delete protection, dedupe and retries.
- Dashboard `python3 checks/school_mail_status.py --verify`: counts-only live
  verification (requires an earlier `--baseline` for read-state equality).
  Never reads finance or transcripts, never prints private response contents.
- The older agent/Discord design below describes the compatibility path, not
  the active job. Do not re-enable `--agent` or Discord for this deployment.

## Historical/optional agent pipeline

- Native script-only cron runs `hermes -p mail mail-collect` every minute. MSAL silent token refresh and Graph pagination/body retrieval are deterministic. Authentication failures never start an interactive login in cron.
- A separate native monitor cron runs `hermes -p mail mail-monitor`, supplies exactly one queued mail, and starts the normal agent only when output changes. Pending attempts carry a new generation so a failed agent cannot lose work to the monitor's pre-run hash snapshot. Empty output is the stable `NO_PENDING_MAIL` marker.
- The agent writes a Japanese summary and calls `mail_publish(batch_id, summary)`. The tool is scoped to the configured mail cron execution. It publishes to the fixed channel through public `hermes send` and records a message ID. Publication and collection never change Outlook read state; a new receipt starts unread.
- Read state changes only when the user explicitly clicks a mail card in the `/mail/` reader. The site then calls Graph `isRead=true`, reads the field back, and only after verification persists the read flag under the archive lock. A failed Graph call or failed read-back is reported as a failure and leaves the receipt unread.
- A successful send with failed read marking is not re-sent. Sending/unknown windows remain held for operator reconciliation, not automatic duplicate delivery. Delivered unread mail stays deduplicated through existing receipts, so repeated collection does not queue it again.
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
- Background collection, summary generation, publication, retries and the reader's list/body GETs never mark mail read. Read mail, sending replies, deleting mail, and attachments are not exposed as tools.
- Full text bodies are retained for the web reader. Only the LLM handoff is truncated at 24000 characters, with an explicit notice. Summary must fit one Discord text message.
- Discord receipt IDs are not exactly-once guarantees. Ambiguous sends remain retained/held.
- `mail_publish` validates ownership and arguments in code. Tool calling by the model is still probabilistic; pending generations permit a later attempt if it omits publication.
- Mail profile retains its existing local qwen3.8-flash-next model. Default profile owns the shared Discord transport; no bot token is copied into mail.

## Sites reader and historical migration

- Confirmed receipts retain `record: {mail, summary}` atomically with the Discord receipt. The existing local cron model and delivery behavior are unchanged; the homepage reads these records without another inference job or copied static data.
- The deployed personal homepage exposes `/mail/`, `/mail/api/data` (summaries, read state and grouping metadata), `/mail/api/body?id=<sha256>` (plain-text body on demand), and `/mail/api/read` (the explicit-click read mutation). The read route requires the custom `X-Mail-Action: mark-read` header, a same-origin `Origin`, and a 64-hex opaque ID. It is not available as GET, and no automatic selection, poll, list or body request marks mail read. The reader shows unread styling, polls every 60 seconds, and reports collection errors separately from retained summaries. `/artifacts/summary-mail/index.html` redirects to the live reader.
- Existing historical receipts that were marked read by the old automatic ACK are mapped to read through their `acknowledged` flag. Receipts are not bulk-rewritten or reset; new receipts carry `read: false` until an explicit click succeeds.
- Mail data is not placed in the shared artifact folder. Existing opaque-origin artifact sandbox restrictions remain intact. The reader is served only through the existing loopback service / Tailnet HTTPS route, with no external assets, analytics, hosted inference, or browser persistence.
- `HERMES_HOME=<mail-profile-home> python backfill.py` recovers already-published summaries from local Hermes tool-call history and retrieves original bodies with read-only Graph GETs. It processes only confirmed delivery receipts, never resends Discord messages or changes read flags, retains a private checkpoint and pre-migration backup, and is idempotent. No message content is printed.
- The one-time migration adapter intentionally depends on the installed Hermes `messages.tool_calls` schema. A missing summary or original fails rather than fabricating content. Normal collection/publication and the web view do not depend on Hermes's session schema. Historical originals reflect the mailbox at migration time; new originals are retained at collection time.
- Commit-to-PR lookups with HTTP 404/422 fall back to sender routing rather than blocking collection. Authentication/rate-limit/server errors remain failures for retry.

## Verification

`python test_runtime.py` runs isolated synthetic fixtures, no real mail writes, Graph calls, Discord posts or site clicks.
`python test_routing.py` covers routing/thread fences without external writes.
Homepage `python -m unittest -v test_mail.py test_serve.py` covers archive mapping, explicit read persistence, endpoint guards and failure status without private runtime state.
Run `python check_mail_read_browser.py` for the synthetic browser fixture; it uses stubbed fetch data and never touches the live mailbox.
`hermes -p mail plugins doctor /home/shi/.hermes/profiles/mail/plugins/mail-monitor --ci` validates real plugin discovery.
Inspect `hermes -p mail cron runs <job>` and durable mail receipts separately; a scheduler success is not itself proof of publication.
Live acceptance requires an actual Discord message readback and a deliberate user read click with Graph isRead readback. Do not automate either against the real mailbox.
