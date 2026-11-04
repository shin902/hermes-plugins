# host-github

A Hermes plugin exposing one `host_github(action, repo, params)` tool for Git, GitHub issues, PRs, and Actions. It invokes `/usr/bin/gh` or `/usr/bin/git` directly with `shell=False`, on the host regardless of the terminal backend. Git uses an explicit absolute **host** checkout/worktree path; GitHub CLI runs in the calling profile's Hermes home. Docker end-to-end behavior remains untested.

It supports repository metadata; listing/viewing/diff/checks for PRs; listing/viewing issues; listing/viewing Actions runs; and creating PRs/issues. Comment writes are intentionally unavailable in v1 because an exact safe comment readback is not implemented. Every call must name an explicit `owner/name`. No generic API, arbitrary command, aliases/extensions, auth operations, merge, close, or delete operations are exposed. The schema's `x-action-parameters` map gives each action's `allowed` and `required` parameters. PR/issue writes require `params.confirm: true`; after a successful write the tool extracts the returned GitHub URL and reads the target back. It verifies exact title/body and, for PRs, exact head/base. Every write response includes `write_succeeded`, `verified`, and `retry_safe`. If the write times out or completion is uncertain, `write_succeeded` is `null`; if readback fails or mismatches, it is `true`; both cases return `verified: false` and `retry_safe: false`. Tool content is not included in the audit file.

## Host Git (no approval prompt)

All Git actions require `params.cwd`, an absolute host checkout/worktree path.
Container paths such as `/workspace` must be translated to their host path by the caller.

| Action | Other parameters | Result |
| --- | --- | --- |
| `git_status` | none | short status and branch |
| `git_diff` | optional `staged` boolean | unstaged or staged diff |
| `git_log` | optional `limit` (1–100) | recent one-line commits |
| `git_add` | `paths`: nonempty relative path list | stage literal paths; read status back |
| `git_commit` | `message` | commit staged changes; read HEAD back |
| `git_fetch` | `branch` | fetch that GitHub branch into `FETCH_HEAD`, without merge/checkout |
| `git_push` | `branch` | push that local branch to the same GitHub branch; read remote ref back |

`repo` remains explicit `owner/name`; fetch/push construct `https://github.com/owner/name.git`.
Git uses the host's existing user name, email, credential configuration, and hooks.
It deliberately runs against the real checkout, not a bundle broker. **Checkout Git
configuration, includes, URL rewrites, credential helpers and hooks are trusted host
inputs and may execute host programs. This is not an isolation boundary against a
malicious checkout.** No extra repo/branch allowlist was added; the existing optional
`HOST_GITHUB_ALLOWED_REPOS` applies to both GitHub and Git actions.

The plugin adds no approval hook for Git or GitHub actions. Git does not require
`confirm`; the existing PR/issue `confirm` parameter is input validation, not a human
approval dialog. Git accepts no arbitrary flags, force/delete push, or shell commands.
Push targets one explicit commit/ref with tag following and submodule recursion off.
Failed/uncertain mutations are not automatically retryable; push success requires an
exact remote-ref readback. Tests push only to a disposable local bare remote.

## Trust and configuration

An optional operator-owned exact-match allowlist can be set in the Hermes process environment as `HOST_GITHUB_ALLOWED_REPOS=owner/repo,other/project`. It is checked in host code and never passed to gh. By default there is no allowlist: the calling host user's gh account can act on any repository to which that account has access; operation restrictions and explicit repository validation remain in force. An administrator should use a dedicated least-privilege host account if narrower scope is needed. The plugin keeps only a minimal subprocess environment and does not inherit debug, host override, pager, editor, browser, or token variables. gh reads its normal host config under `/home/shi/.config/gh`, so existing host authentication is used without copying or exposing token contents. Authentication availability depends on that host configuration being valid.

Audit metadata (`time`, validated action, validated repo, success) is appended under this plugin directory's `audit/calls.jsonl`, with private directory/file permissions. Output is captured by concurrent pipe-draining threads with a per-stream cap. Timeouts terminate, then kill if needed, and reap the child process. Errors and returned successful text are redacted; successful JSON is not truncated by redaction. No content or credentials are audited.

## Install and enable

Install it in any profile's plugin directory (`~/.hermes/plugins` for the default profile, `~/.hermes/profiles/<name>/plugins` for a named profile) and keep it disabled until opted in. Enable with:

```sh
hermes plugins enable host-github
```

Then restart that profile's Hermes process to load the plugin. Do not change terminal/sandbox configuration for this plugin. Check current CLI syntax with `hermes plugins --help` before enabling if the installed CLI differs.

## Verify

```sh
python -m unittest discover -s <this-directory>/tests -v
hermes plugins doctor <this-directory> --ci
```

Local unit fixtures cover validation, write readback and retry semantics, redaction, output bounds, and timeout cleanup. They use local child processes and make no network requests or GitHub writes. Network, host auth, and install-lock failures describe those environment restrictions; they do not indicate a plugin host failure. The Docker end-to-end path is untested.

A read-only live smoke check can call `host_github` with `action=repo_view`, `repo=NousResearch/hermes-agent`, and `params={}`. This only works when `/usr/bin/gh` can reach GitHub and its existing host authentication/configuration is usable. Tool registration uses the documented `register(ctx)` and `ctx.register_tool(...)` API.
