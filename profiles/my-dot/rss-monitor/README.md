# RSS monitor v3 — public plugin pipeline

No changes to Hermes core; no monkey patches; no added daemon.

1. One standard script-only cron invokes `hermes rss-collect` every 15 minutes. Each feed gets one original-URL request, no immediate retry/fallback/cache-buster. The collector records individual failed fetches in `cron/rss-monitor/<job>-health.json` and `logs/cron-rss/<job>.jsonl`.
2. Each existing monitor cron invokes `hermes rss-monitor --job-id <id>`; this ONLY prepares queued items, never fetches. Unpublished batches get a fresh attempt generation so standard cron's pre-agent hash snapshot cannot swallow a retry. After confirmed publication the output stays stable, suppressing future LLM calls while empty.
3. The Hermes agent researches/summarizes with its usual memory/session context, then calls `rss_publish(batch_id, summary)`. The tool checks public cron execution identity; the target is fixed in `delivery.json`.
4. The tool persists a `sending` fence before any Discord transport, invokes public `hermes send --json`, records every returned message ID, and acknowledges only after every chunk succeeds. Standard cron final-answer delivery is `local`, preventing double posting. Agent/state/config failures still use native `failure_deliver`.

## Important guarantees/limits

- `seen_ids` means ever collected/deduplicated, NOT read/delivered. Unread work is retained in `pending` or `current`; delivery has a separate receipt.
- `sending` / `unknown` holds the batch; do not automatically reset or resend. Resolve against actual Discord message IDs/content before human-approved acknowledgement or retry. This is NOT exactly-once delivery.
- Queue acceptance, a warning/partial response, an absent/non-string message ID, a failure notification, or a completed cron run are NOT article delivery receipts.
- Transient 404/500/timeouts are failed in plugin health/HTTP diagnostics but quiet in chat. The collector cron's success means it performed and recorded collection attempts, not that every feed succeeded. Per-feed HTTP status is no longer the per-feed native execution status; dispatcher executions only describe preparing/summarizing/publishing.
- Continuous outage permits ONE notification ATTEMPT after three hours. Its confirmed/unknown result is stored separately. It is not guaranteed delivery; ambiguous notification sends are not retried automatically. A notification error fails the collector into the native failure lane. A successful fetch resets that feed's outage without a recovery chat message.
- Parse/config/state/summary/delivery errors are not treated as ordinary transient fetch failures. A changed nonce retries an agent that never publishes; an agent that falsely reports success cannot discard unread work.
- Legacy unverifiable prior articles stay quarantined in existing legacy fields and backup files, not acknowledged or automatically reposted.

## Configuration and profile

Install in the intended `$HERMES_HOME/plugins/rss-monitor/`, with `feeds.json` (job ID to original URL) and `delivery.json` (same job IDs to exact `discord:<channel>:<thread>` target). `get_hermes_home()` scopes all state; subprocess `HERMES_HOME` is explicitly pinned. Enable only in the intended profile, using `hermes plugins enable rss-monitor`.

Do not let the model edit state or split send and read operations. Generic send_message tools are not an alternative to the dedicated publish tool. Keep standard cron automatic delivery local.

## Shorts policy

`policies.json` enables `exclude_youtube_shorts` only for the AI整体師 job. Preparation deterministically removes YouTube URLs whose parsed path starts with `/shorts/` from pending/ready batches; titles, descriptions and hashtags do not affect classification, records policy exclusions separately from delivery, and emits stable empty output when nothing remains. Mixed batches retain normal videos; modified ready batches get a new ID to reject stale publishers. Unknown/sending batches are never discarded. Publication independently rejects an unfiltered Shorts batch. Other jobs retain their existing behavior. This does not guess Shorts from video duration; unmarked watch URLs cannot be classified by this filter alone.

## Verification

- `python test_runtime.py`: isolated state, transport mocks, crash/partial/unknown fence, receipt validation, current-cron ownership and fixed targets, stable empty output, single-attempt/outage policy, diagnostics redaction.
- `python verify_contract.py`: isolated plugin CLI discovery/doctor/preparation, plus actual unmodified HEAD cron monitor functions proving retry after agent failure and no_change after acknowledgement. No Discord messages sent by these tests; receipt IDs are explicitly test fixtures.
- Live verification must separately execute actual collector/dispatcher jobs and read back their native execution ledger, plugin state and real transport receipt. A no_change run does not prove an article was published.
