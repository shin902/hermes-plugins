# Managed profile plugin deployment

Python 3.9+ standard library only; no network, plugin imports, config changes or process restarts.

    python3 setup_profiles.py --home /home/shi/.hermes --dry-run
    python3 setup_profiles.py --home /home/shi/.hermes --apply
    python3 test_setup_profiles.py -v

Dry-run is the default. --home overrides HERMES_HOME; --repo and --mapping are configurable.
The schema_version 2 manifest explicitly maps each profile/plugin install to a
repository-relative source and exact version. There are no pending or record-only
entries. Existing correct links remain unchanged. Missing links are relative;
existing real directories or divergent links are refused. Validation precedes all
writes and partial link creation rolls back. Output is aggregate-only.

Each plugin has exactly one repository source: its plugin path at the root
(including model-providers/opencode-acp). Every profile maps to that same path;
the setup script rejects profile-specific source paths to prevent new forks.
RSS feeds.json, policies.json and delivery.json live outside the plugin symlink,
under each profile's HERMES_HOME/plugin-config/rss-monitor/, untracked.
Durable RSS state remains under that profile's cron/rss-monitor/.
Host audit directories remain beside their source and writable at the same
__file__.resolve()-relative location. Other runtime/generated files stay intact,
but are excluded from Git. A checkout alone intentionally does not reconstruct
private runtime configuration: restore that profile's files from a local backup
before enabling services on a new machine.

The original import retained per-profile variations. Source unification now uses
the root bot-acl implementation (including unlimited consultation time and resume)
for all mapped profiles. Identical Orca and Activity copies were deduplicated;
single-profile plugins and the custom provider were relocated without code changes.
RSS processes exactly one article per run, with profile-local Shorts exclusion policies.
hermes-activity is ordinary source, not a nested Git repository.
Unification does not change activation settings or restart Hermes.

Full pre-cutover hash/mode-verified snapshots and retired originals are outside
this repository under the Hermes home, in hermes-plugin-migration-* directories.
Directory-to-link cutovers used Linux renameat2(RENAME_EXCHANGE), leaving the
install path continuously present; changes detected between snapshot and cutover
were reconciled for non-source files. Runtime writes after cutover naturally use
the symlink destination. Long-lived already-open descriptors can still refer to
retired inodes; originals are retained for recovery rather than deleted.

The setup script accesses only explicit manifest entries; it does not enumerate
profile trees, tombstones or hidden backup profiles. Five manual plugin names
(hindsight, homeassistant, memory-wiki, talk-desktop, spotify) are refused and
were verified unchanged during migration.

Git ignores runtime JSON/configuration, audit/state/log/cache directories, secrets,
backup snapshots, nested Git metadata and dependencies. Source and setup metadata
are staged; review locally before committing. Keep manifest details local.
