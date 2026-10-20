# Hermes managed plugins

Central, offline Git-managed source for active-profile native plugins.
See SETUP.md for deployment, state isolation, verification and backup recovery.
profile-plugins.json is the explicit installation/source/version manifest.
The repository includes ordinary source for activity and custom model providers.
Runtime state, credentials and migration backups are intentionally not tracked.
Each plugin has one canonical source at its root plugin path; all profiles link
to that source. RSS configuration lives in each profile's plugin-config/rss-monitor/.

    python3 setup_profiles.py --home /home/shi/.hermes

This validates without modifying anything. Use --apply to deploy missing links;
correct existing links are idempotent. Manual plugin exceptions are untouched.
