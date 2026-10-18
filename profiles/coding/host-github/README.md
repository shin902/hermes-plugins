# host-github

A coding-profile Hermes plugin that exposes one bounded `host_github(action, repo, params)` tool. It invokes `/usr/bin/gh` directly with `subprocess.Popen(..., shell=False)` from a fixed coding-profile working directory. It never invokes terminal tools or local git. Therefore the gh subprocess runs on the host independent of Hermes' terminal backend. Docker end-to-end behavior remains untested.

It supports repository metadata; listing/viewing/diff/checks for PRs; listing/viewing issues; listing/viewing Actions runs; and creating PRs/issues. Comment writes are intentionally unavailable in v1 because an exact safe comment readback is not implemented. Every call must name an explicit `owner/name`. No generic API, arbitrary command, aliases/extensions, auth operations, merge, close, delete, push, or local paths are exposed. The schema's `x-action-parameters` map gives each action's `allowed` and `required` parameters. Writes require `params.confirm: true`; after a successful write the tool extracts the returned GitHub URL and reads the target back. It verifies exact title/body and, for PRs, exact head/base. Every write response includes `write_succeeded`, `verified`, and `retry_safe`. If the write times out or completion is uncertain, `write_succeeded` is `null`; if readback fails or mismatches, it is `true`; both cases return `verified: false` and `retry_safe: false`. Tool content is not included in the audit file.

## Trust and configuration

An optional operator-owned exact-match allowlist can be set in the Hermes process environment as `HOST_GITHUB_ALLOWED_REPOS=owner/repo,other/project`. It is checked in host code and never passed to gh. By default there is no allowlist: the calling host user's gh account can act on any repository to which that account has access; operation restrictions and explicit repository validation remain in force. An administrator should use a dedicated least-privilege host account if narrower scope is needed. The plugin keeps only a minimal subprocess environment and does not inherit debug, host override, pager, editor, browser, or token variables. gh reads its normal host config under `/home/shi/.config/gh`, so existing host authentication is used without copying or exposing token contents. Authentication availability depends on that host configuration being valid.

Audit metadata (`time`, validated action, validated repo, success) is appended under this plugin directory's `audit/calls.jsonl`, with private directory/file permissions. Output is captured by concurrent pipe-draining threads with a per-stream cap. Timeouts terminate, then kill if needed, and reap the child process. Errors and returned successful text are redacted; successful JSON is not truncated by redaction. No content or credentials are audited.

## Install and enable

This directory is installed only in `~/.hermes/profiles/coding/plugins/host-github`; keep it disabled until opted in. From the coding profile, enable with:

```sh
hermes --profile coding plugins enable host-github
```

Then restart the coding-profile Hermes process to load the plugin. Do not change terminal/sandbox configuration for this plugin. Check current CLI syntax with `hermes --profile coding plugins --help` before enabling if the installed CLI differs.

## Verify

```sh
python -m unittest discover -s ~/.hermes/profiles/coding/plugins/host-github/tests -v
hermes --profile coding plugins doctor ~/.hermes/profiles/coding/plugins/host-github --ci
```

Local unit fixtures cover validation, write readback and retry semantics, redaction, output bounds, and timeout cleanup. They use local child processes and make no network requests or GitHub writes. Network, host auth, and install-lock failures describe those environment restrictions; they do not indicate a plugin host failure. The Docker end-to-end path is untested.

A read-only live smoke check can call `host_github` with `action=repo_view`, `repo=NousResearch/hermes-agent`, and `params={}`. This only works when `/usr/bin/gh` can reach GitHub and its existing host authentication/configuration is usable. Tool registration uses the documented `register(ctx)` and `ctx.register_tool(...)` API.
