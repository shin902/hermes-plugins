CURRENT POLICY: arbitrary Hermes CLI argv with a narrow direct-route denylist

No root/action allowlist. Unknown/plugin roots and unknown flags pass unchanged.
Cross-profile config/profile management passes. console and send pass because
neither is itself an agent-model entrypoint. Input shape/execution contract is
unchanged. The previous broader restrictions were our design error after the
user changed the original narrow scope; they are not the current agreement.

Denied known dispatches
  No command (including known global-option/profile-only invocation).
  Global oneshot; chat/rl; desktop/gui; proxy/acp; dashboard/serve server launch.
  MCP serve; peer dm/run; cron run/tick; curator run.
  Gateway bare/run/start/restart/install (backend launch or startup enablement).
  Gateway migrate except --dry-run: convergence can restart the default backend.
Allowed lifecycle branches
  Help-only direct routes, global help/version, dashboard/serve --status/--stop,
  dashboard register; gateway help/status/list/stop/uninstall/setup/migrate-legacy,
  gateway enroll and future administrative actions (not a family allowlist).
  TUI/CLI/resume/continue options only deny when they resolve to a chat launch;
  they do not ban an otherwise ordinary explicit administration command.

Argument boundaries
  Installed _parser.py global option arity is recognition metadata, not an
  allowlist. Known option values, equals forms, short attached values/clusters,
  unique long abbreviations and -- are handled without rewriting executed argv.
  Admin payloads and mcp add --args payloads are not scanned for chat strings.
  Unknown prefix-option arity cannot be safely inferred. Such invocations pass
  through, so this classifier cannot guarantee denial if unknown options are
  combined with agent launches. Future plugin/launcher semantics need review.

Explicit launcher exception
  Leading --run-module is rejected for ALL modules, including non-chat modules.
  The launcher dispatches arbitrary Python before CLI parsing; modules can wrap
  or invoke chat and there is no public safe classification contract. This is
  the precise non-chat passthrough exception, not a hidden admin allowlist.
  --print-runtime-command is allowed. No launcher/core edits were made.

Source basis
  hermes_cli/_parser.py global flags; main.py:2876-2880,3603-3610;
  subcommands/dashboard.py:39-45,103-125; gateway.py:5742-5750;
  gateway_migrate.py service convergence; console_engine.py curated command REPL;
  _launchers.py leading module dispatch. This is direct route prevention, NOT
  a security sandbox: plugin commands, helpers, config and indirect code can
  launch AI. Unknown plugin behavior is intentionally not blanket prohibited.

Verification
  Full test discovery exercises mocked pre-subprocess denials and byte/order-
  preserving custom/future commands, cross-profile config set (mock only), flags,
  chat-looking values, help/lifecycle operations and process contract.
  Fresh Plugin Doctor imports and registers one tool. Safe native version and
  -p default config get terminal.backend succeed. Current native handler can be
  cached; a NEW native agent-session activation is not yet verified in this
  bounded consultation (ask_bot is not exposed). No prompts, UI/server launches,
  messages, real config writes, Gateway restarts or network/mount edits tested.

HISTORICAL ARCHIVE BELOW — superseded, not the current policy.

Hermes administration route guard

Supersedes README.md's historical profile-lock policy. The public tool name and argv-only JSON shape are unchanged. Caller must still be coding; targets may be any profile. No profile selectors are injected, normalized or removed from the executed argv.

Installed host --version and --help inspected: v0.22.2, local d923a5eb487a. Source inspected: hermes_cli/main.py dispatch and profile pre-parser, _parser.py global flags, subcommands/gateway.py, mcp.py, peer.py, console.py, sessions.py and send_cmd.py.

Blocked: bare/options-only/profile-only invocation; chat in all modes including -q/-Q/--query; global oneshot, resume/continue, --tui/--cli paths; desktop/gui, dashboard, serve, proxy, acp, console, send; mcp serve; peer dm/run; bare gateway and gateway run/start/install/restart/migrate/enroll; cron run and curator run. Unknown/plugin top-level commands and unrecognized/abbreviated/attached global options fail closed. Surface-family help is conservatively denied; isolated --help and --version are allowed.

Rationale: serve/dashboard/desktop are chat front/backends. ACP and MCP serve expose agent conversations. Proxy exposes inference prompts. Gateway launch/restart/install can activate conversation adapters; install can start immediately or at boot, so even no-start install is denied. Send is outbound delivery rather than an LLM turn itself, but enables interaction/messages to agents/platforms, so denied. Peer dm/run explicitly perform remote agent turns. Console is an unnecessary alternate dispatch surface and conservatively denied.

Allowed: explicit installed administration roots listed in ADMIN_COMMANDS, including all profile management, config get/set/edit, ordinary auth/model/tools/plugins/settings commands. Gateway restricted to status/list/stop/uninstall/setup/migrate-legacy. MCP restricted to administration verbs in RESTRICTED (bare MCP opens a catalog picker, not agent chat). Peer registry administration/status/stop allowed. Non-chat globals with known arity may precede admin commands. Argument values such as 'chat' in config set remain data. subprocess retains fixed executable, shell=False, DEVNULL stdin, sanitized environment and timeout.

Tests: python -m unittest discover -s /home/shi/.hermes/profiles/coding/plugins/coding-admin -p 'test_*.py' -v
All mutation-looking allowed test inputs use mocked subprocesses. Rejection tests assert subprocess was not called. Historical tests asserting the reversed profile lock are retained as test_admin.py.legacy-profile-lock, outside test discovery.

Residual risks: not an OS sandbox. Intentionally unrestricted configuration, plugin installs, MCP discovery/test commands, editor/helper commands, scheduled job configuration and arbitrary admin behavior can execute code or indirectly enable AI. Settings can also select container routing. This policy is direct route prevention, not adversarial denial of all indirect execution. New subcommands within allowed admin families require review on Hermes upgrades. No claim that this is a complete source-level audit of every indirect invocation.

Live verification limitation: this running consultation's native tool schema/handler remains the OLD registration. Native --version succeeds; native -p default config get terminal.backend returns a generic host/input error. An empty chat -q probe unexpectedly launched the classic REPL and immediately exited on DEVNULL without a prompt or model turn; session 20261017_225855_d26dd3 was created. No further live chat/server/UI probes were attempted. Fresh native activation and required read-only cross-profile check are NOT verified; local fresh registration and mocked guards are verified only. No config, mount, network, gateway-state changes or service restarts were performed by the implementation; the unintended empty REPL may have written session history.
