CURRENT CONTRACT — arbitrary Hermes CLI argv passthrough

hermes_cli takes only argv (array of NUL-free strings). Arbitrary installed
Hermes CLI roots, plugin commands, future commands and ordinary flags/values
are forwarded unchanged in order. Cross-profile selectors and profile management
are allowed. No ADMIN_COMMANDS allowlist or confirm parameter. Caller is coding;
internal plugin/toolset identity is hermes-cli. Activation uses plugins.enabled.

Known direct chat/agent-launch routes are denied, not unknown administration.
console/send pass. Help/status/stop lifecycle exceptions pass. See
CONVERSATION-GUARD.md for precise exclusions and recognition limits.

Fixed Hermes executable, shell=False, DEVNULL stdin, sanitized environment,
600-second timeout and stdout/stderr/exit_code/timed_out contract are unchanged.
Nonzero CLI exits are returned, timeout mutation outcome is unknown, and output
is not secret-redacted. CLI helpers/plugins can execute host code: not a sandbox.

Verification uses mocked subprocesses for all mutations and chat routes, plus
Plugin Doctor and safe native version/default terminal.backend reads. Fresh
Plugin Doctor registration is not the same as fresh agent-session activation.
No Gateway restart, config, mount or network changes are part of this update.

HISTORICAL ARCHIVE BELOW — NOT CURRENT BEHAVIOR.
The old forced-profile instructions and verification claims below are obsolete.

LEGACY DOCUMENT — superseded by CONVERSATION-GUARD.md.
The profile lock and forced -p coding described below have been REMOVED.
Do not use this historical verification procedure; see the new guard document.

Hermes CLI — historical host Hermes CLI

The standard public plugin API exposes hermes_cli only to the coding profile.
The registered toolset id is coding-admin, matching the existing selected toolset.
The manifest name is also coding-admin: installed discovery uses the manifest
name as the activation key for flat plugins, not the directory name. The exposed
schema name remains hermes_cli. No config change is required.
The regular installed Hermes CLI and launcher are unchanged. hermes-admin is
not needed and remains disabled.

Usage
  {"argv":["--version"]}
  {"argv":["-p","coding","config","path"]}
  {"argv":["-p","coding","config","get","terminal.backend"]}

argv is an argument list, not a shell command. Every invocation forces -p coding.
Separate -p/--profile and --profile= values are validated across the entire argv;
coding selectors are normalized (strip/casefold) and removed before injection.
Alternate, missing and attached short selectors are rejected before subprocess.
Selector tokens are checked even after -- and in MCP passthrough regions.
Other argument strings retain their boundaries and order; a selector substring
inside ordinary text is not rejected. Empty argv is accepted (noninteractive).
There is no additional subcommand allowlist or confirmation field.
Malformed argv (non-list, non-string items, embedded NUL) is rejected.
Unknown tool fields are rejected.

Execution
  Fixed executable: /home/shi/.hermes/hermes-agent/.hermes/bin/hermes
  shell=False; stdin=DEVNULL; cwd=/home/shi/.hermes/profiles/coding
  Explicit host environment: HOME=/home/shi, USER/LOGNAME=shi,
  PATH=/usr/bin:/bin, LANG=C.UTF-8, HERMES_HOME=/home/shi/.hermes,
  XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS for the host UID.
  No inherited environment credentials, Python injection variables or PATH.
  The CLI loads its normal profile environment/credentials itself.
  -p coding is always injected; alternate profile selectors cannot override it.
  600-second timeout; stdout/stderr use UTF-8-compatible replacement decoding.

Returns JSON with ok (tool dispatch succeeded), stdout, stderr, exit_code and
 timed_out. A nonzero CLI exit is returned intact, not raised or suppressed;
check exit_code rather than ok for CLI success. On timeout exit_code is null,
partial output is retained, and mutation outcome is explicitly unknown.
No environment dump, extra log reads or exception diagnostics are returned.
Normal CLI stdout/stderr is NOT secret-redacted: commands that ordinarily reveal
secrets still do so. This interface adds no secret output beyond the CLI itself.

Authority and consequences
This guard enforces coding profile selection, not full cross-profile isolation.
Hermes commands with positional profile targets are not scoped by -p coding;
therefore exact profile/profiles argv tokens are also denied before execution,
including profiles delete default. Other shared/global administration is not
prohibited by this selector/family guard.
There is no arbitrary-shell or arbitrary-executable parameter: the executable is
fixed, shell=False. This is not an OS-level host-code execution boundary.

Verification
  hermes --run-module unittest discover -s
    /home/shi/.hermes/profiles/coding/plugins/coding-admin -p 'test_*.py' -v
  hermes -p coding plugins validate <this-directory> --json
  hermes -p coding plugins doctor coding-admin --ci

Only required argv is exposed. Tests mock all mutations and cover argument
preservation, process boundary, nonzero output, timeout output, malformed inputs,
environment and caller guard. Twelve alternate/malformed selector cases assert
subprocess.run is never called. Real read-only version/config subprocess tests
mock only the caller-home check because launcher module mode bypasses profile
selection; the subprocess and its forced -p coding selection are real.
The fresh registration schema is printed and checked for hermes_cli / argv only.

Existing processes/conversations may retain previously loaded code/tool schemas.
Use a newly loaded session/process for the new registration. No gateway restart
or forced reload is performed by this change.
