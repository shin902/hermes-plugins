# Host Android Hermes plugin

This plugin registers one compact tool, `host_android(action, params)`, through
`ctx.register_tool`. It runs fixed host binaries with a restricted environment.
It does not expose shell, arbitrary argv, `adb shell` commands, Orca `exec` or
`eval`, terminal creation, or process killing.

## Actions and exact parameters

Every action rejects unknown fields. `confirm: true` is a required explicit
intent field for actions that can change device state; it is not a substitute
for user authorization in an agent workflow.

| Action | Required params | Optional params | Effect |
|---|---|---|---|
| `devices` | none | none | Read-only ADB enumeration and Orca device list. Orca errors are included separately. |
| `avds` | none | none | Lists only the allowlisted AVD `MyJune_API_36`. |
| `attach` | `device`, `workdir`, `confirm` | none | Orca attach helper; may start/attach a device. Accepts an `emulator-N` serial or allowlisted AVD name `MyJune_API_36`. |
| `ax` | `device`, `workdir` | none | Read AX tree through Orca. |
| `screenshot` | `device` | none | Saves a PNG under `workspace/android-evidence/`. |
| `tap` | `device`, `x`, `y`, `workdir`, `confirm` | none | Orca normalized coordinates, each 0–1. |
| `type` | `device`, `text`, `workdir`, `confirm` | none | Safe ASCII letters, digits, space, `_.,!?@#%&()+:=/`, at most 128 characters. Shell metacharacters, quotes, dashes, and controls are rejected because the installed Orca transport backend is opaque. |
| `key` | `device`, `key`, `confirm` | none | Finite key set: `back`, `home`, `menu`, `enter`. |
| `launch` | `device`, `package`, `confirm` | none | Launches only `com.shi0203.MyJune`. |
| `force_stop` | `device`, `package`, `confirm` | none | Stops only `com.shi0203.MyJune`. |
| `install` | `device`, `apk_path`, `confirm` | none | Installs an existing APK under the MyJune repo/worktrees; no build is run. |
| `reverse` | `device`, `port`, `confirm` | none | Creates `adb reverse` for port 8081 or 8082. |
| `app_state` | `device`, `package` | none | Returns bounded matching lines from activity dumpsys. |
| `record_start` | `device`, `duration`, `confirm` | none | Starts a naturally bounded (1–180s) screenrecord worker. Returns an opaque UUID. |
| `record_status` | `job_id` | none | Reads worker status; never kills a worker. |
| `record_collect` | `job_id` | none | Only after clean worker completion, pulls and validates MP4, then returns the absolute path. |

Device actions other than attach require `emulator-[0-9]+`. Orca actions require
an explicit `workdir`: aliases `myjune`, `myjune-worktrees`, or `coding`, or a
validated absolute directory under `/home/shi/orca/workspaces/MyJune`, the
MyJune repository, or the coding profile. Symlinks and resolved escapes are
rejected. The resolved directory is passed to Orca as an explicit worktree path.
Android SDK, Java, AVD, Orca, ffmpeg, and ffprobe paths are fixed in code.

## Recording lifecycle

Recording state is stored with mode 0600 under
`workspace/android-evidence/.host-android-jobs/`; evidence directories use
mode 0700. A detached Python worker owns a fixed ADB `screenrecord
--time-limit` operation and finishes naturally. The worker identity is checked
using `/proc` start time to detect stale jobs without signaling a reused PID.
Per-device persistent lock files use `flock` and serialized ownership updates
to prevent concurrent recordings without stale-unlink races. Collection is unavailable
until worker exit 0 has been recorded. The pulled file is limited to 256 MiB,
must contain an MP4 video stream with a valid duration/codec, and must pass a
full ffmpeg decode pass. Android screenrecord evidence is silent; audio is not
verified.

## Environment and trust

ADB runs with fixed HOME/Android variables and no inherited credentials.
Orca receives only local runtime selectors (`XDG_RUNTIME_DIR`, D-Bus/display
selectors when present) and the documented optional `ORCA_ENVIRONMENT` or
`ORCA_PAIRING_CODE`; other environment variables are not forwarded. Remote
selectors are credential-like: the plugin does not put them in job manifests
or tool results. The accepted whole `.hermes` Docker mount still grants the
container broad access to Hermes data; this plugin does not narrow that mount.

The configured paths and allowlists are intentionally specific to this host.
The absolute APK path must remain inside one of the configured MyJune trees;
symlinks and traversal are rejected. `confirm=true` is only an input guard and
must not be treated as human consent on its own. Do not call state-changing
actions unless the user has authorized that operation.

## Activation (not executed)

The plugin is not enabled by this artifact. To activate later, add
`host-android` to `plugins.enabled` in the coding profile's Hermes config and
restart/reload Hermes. This task did not change configuration.

## Verification scope

Run tests with:

```sh
python -m unittest discover -s plugins/host-android/tests -v
```

Plugin doctor may be run with `hermes --profile coding plugins doctor
plugins/host-android --ci`. A real device MP4 end-to-end check is intentionally
not claimed: the emulator was shut down during verification. Tests generate a
synthetic color clip with the configured ffmpeg binary and run the actual
ffprobe/full-decode verifier; this is media validation only, not emulator
evidence.
