# Hermes plugins

Source code for the native Hermes plugins used across profiles. Code only: which
profile installs or enables which plugin is configuration and lives in the private
`hermes-config` repository (each profile's `config.yaml` `plugins.enabled`/`disabled`,
plus the `plugins/<name>` symlinks into this checkout).

Each plugin has one canonical source at its root path (including
`model-providers/opencode-acp`); every profile links to that same directory.
Runtime state, credentials, audit logs and per-profile configuration (for example
`plugin-config/rss-monitor/`) stay outside Git. `omh` and `hermes-activity` are
installed from their upstream sources through the Hermes catalog, not from here.

Install into a profile and enable it:

    ln -s ~/ghq/github.com/shin902/hermes-plugins/<plugin> <profile-home>/plugins/<plugin>
    hermes -p <profile> plugins enable <plugin>

Checks:

    python3 -m unittest discover -s secbrief -p 'test_*.py' -v
    hermes plugins validate ./<plugin>
    hermes plugins doctor ./<plugin> --ci
