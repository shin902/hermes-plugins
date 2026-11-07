# host-strata

Drives a local Strata checkout from the coding sandbox: stop the
GPU proxy, serve a chosen engine config, run CMake builds and test suites, and read
their logs. Everything long-running becomes a transient **systemd user unit**, so
stopping a unit kills its whole cgroup (engine children included) and the last exit
status stays readable.

## No approval — by design

This tool runs host commands **without human approval**. `configure`, `build` and
`test` execute code from the checkout, and `serve_start` loads the engine config on
the host as user `shi`. Anyone who can write the checkout or an allowed engine config
can therefore run code as that user. This is an accepted tradeoff for the coding
sandbox; the checkout and engine config dirs are the trust boundary.

## Configuration

Read on every call from `<default HERMES_HOME>/plugin-config/host-strata/config.yaml`
(never committed). A missing file, or a missing `repo`/`proxy_service`/`port`,
fails every call closed.

```yaml
repo: /path/to/Strata                   # required: main checkout; its worktrees are allowed too
proxy_service: llama-swap               # required: systemd --user unit normally holding the GPU
port: 8095                              # required: port for serve_start
python: .venv/bin/python                # relative to repo, or absolute
build_dir: build                        # default build dir name
build_jobs: 8
engine_config_dirs: [/path/to/models]   # allowed dirs for engine JSON configs
default_engine_config: /path/to/models/engine.json
env: {}                                 # extra env for units; {checkout} expands
cmake_args: []                          # configure args; {checkout} expands
lib_dirs: []                            # LD_LIBRARY_PATH for test units and python_cli
```

State and logs: `<default HERMES_HOME>/plugin-state/host-strata/`
(`<unit>.json` records, `logs/<unit>-<timestamp>.log`).

## Actions

| action | does |
| --- | --- |
| `status` | proxy state, serve unit + log record + `/health`, job unit + exit code |
| `proxy_stop` / `proxy_start` | stop the GPU service / start it and wait until active |
| `serve_start` | refuse if proxy runs; launch `serve/server.py --engine strata` on `port` |
| `serve_stop` | stop the serve unit and reset its failed state |
| `configure` | `cmake -S <checkout> -B <checkout>/<build_dir> [+ cmake_args]` |
| `build` | `cmake --build <build_dir> -j <build_jobs> [--target ...]` (needs CMakeCache) |
| `test` | `ctest --output-on-failure [-R filter]`, or `unittest discover -s serve` |
| `job_stop` | stop the build/test unit |
| `log` | last N lines of the recorded log for `unit` serve\|job |

`ctest` is refused while `serve` runs (GPU contention). Only one job at a time.
Checkouts must be `repo` or a path from `git -C repo worktree list`.

## `python_cli`

Runs the configured `.venv` interpreter (`python` key) with arbitrary `args` in a
waiting transient unit (`systemd-run --wait --pipe`, `RuntimeMaxSec=timeout`,
default 600 s, max 3600 s) and returns `exit_code` plus the last 64 KiB of output.
Same environment as `test` units (`env`, `lib_dirs`). Typical uses:
`['-m', 'pip', 'install', '-r', 'requirements.txt']` after an upstream merge, or
`['tools/strata_tokenizer.py', ...]`. Like `host_strata`, it runs **without approval**.

## Typical flow

1. `host_strata(action='proxy_stop')`
2. `host_strata(action='serve_start', engine_config='/path/to/engine.json')` —
   the serve record (`<root>/plugin-state/host-strata/host-strata-serve.json`)
   captures provenance: git HEAD/describe, engine config and exe SHA-256
3. poll `status` until `serve.health` is true
4. run the benchmark **from the sandbox** against the host port; results go to
   `<results-dir>/<timestamp>-<label>/{raw.jsonl,summary.json,meta.json}` with the
   provenance embedded in `meta.json`:

   ```bash
   python3 host-strata/strata_bench.py bench --url http://127.0.0.1:8095 \
     --results-dir /path/to/bench-results --label before \
     --provenance <root>/plugin-state/host-strata/host-strata-serve.json
   ```

5. `serve_stop`, then `proxy_start`
6. after a change, repeat with `--label after` and
   `python3 host-strata/strata_bench.py compare <before-dir> <after-dir>`

## Checks

```bash
python3 -m unittest discover -s host-strata/tests -v
hermes plugins validate host-strata
hermes plugins doctor host-strata --ci
```
