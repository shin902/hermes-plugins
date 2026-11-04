# AGENTS.md — hermes-plugins

Out-of-tree Hermes plugins. They must run on an unmodified Hermes release.

## Rules

- Never modify, patch, vendor, or monkey-patch Hermes source or the installed
  package.
- Never import or call Hermes internals: undocumented modules, private names,
  core registries, or runtime rebinds. Use only documented plugin surfaces —
  `plugin.yaml` + `register(ctx)`, `ctx.*`, documented hooks/ABCs, and the
  Desktop SDK.
- If the public surface is missing, do not reach into core. Report the missing
  upstream seam and leave the feature blocked.
- Do not add new private imports. Existing ones are migration debt; remove the
  one in a file you touch.

## Verify

```bash
hermes plugins validate <plugin-dir>
hermes plugins doctor <plugin-dir> --ci
```

No `private core imports` (new or increased) and no `no core override` findings.
Run the plugin's own tests (`test_*.py` beside it or under `tests/`).
Which profile installs or enables a plugin is configuration: keep it in the
private `hermes-config` repository, never in this public repository.
