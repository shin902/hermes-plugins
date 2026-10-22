# Artifacts

`artifact_register` copies an explicitly selected folder of HTML, Markdown and
assets into `~/.hermes/sites/homepage/artifacts/<slug>/`. The personal homepage
reads `artifact.json` in each folder to generate its `/artifacts/index.html`
cards. The homepage server is an existing deployment, not started by this plugin.

The default and coding profiles link to this single plugin source through
`profile-plugins.json`; published files remain outside Git and outside both
profile plugin directories. Returned URLs are site-relative so no personal
Tailnet hostname is committed to this public repository.

Parameters: `source` (absolute folder), `slug` (unique lowercase letters, digits,
hyphens), `title`, optional `description`, and `entry` (default `index.html`;
Markdown is also supported). Only publish folders the user explicitly selects.
Hidden files, symlinks and overlapping source/destination folders are refused;
existing slugs are never overwritten.

Run `python artifacts/test_artifacts.py` and `hermes plugins doctor artifacts`.
The homepage's `check_artifacts.py` verifies the catalog and Markdown integration.
