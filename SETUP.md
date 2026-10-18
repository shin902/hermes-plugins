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

Sources are profile-specific unless an already-correct shared link existed.
RSS feeds.json, policies.json and delivery.json remain with their own profile
source, untracked. This preserves __file__-relative lookup and delivery isolation.
Host audit directories remain beside their source and writable at the same
__file__.resolve()-relative location. Other runtime/generated files stay intact,
but are excluded from Git. A checkout alone intentionally does not reconstruct
private runtime configuration: restore that profile's files from a local backup
before enabling services on a new machine.

Migration used the newest semantic version already installed in active profiles.
Equal-version local variations were retained per profile. Existing custom provider
code was preserved. hermes-activity is ordinary source, not a nested Git repository.
No upstream fetches, package installs, commits, pushes or Hermes restarts were done.

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
