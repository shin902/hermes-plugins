#!/usr/bin/env python3
"""Post-reset priming and one JSON-queued task per five-hour window."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import time

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
ROOT = Path(__file__).resolve().parent
STATE = HOME / "plugin-data/codex-usage/actions.json"
QUEUE = HOME / "plugin-data/codex-usage/tasks.json"
CODEX = os.environ.get("CODEX", str(Path.home() / ".local/bin/codex"))
PRIME_CWD = HOME / "cache/scratch/codex-usage-prime"
DEFAULT_TASK_MODEL = "gpt-6.1-sol"
spec = importlib.util.spec_from_file_location("codex_usage_monitor", ROOT / "monitor.py")
assert spec and spec.loader
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def active_five_hour(s):
    rows = [w for w in s.get("fiveHour", []) if w.get("windowDurationMins") == 300]
    return rows[0] if len(rows) == 1 else None


def eligible(window, now):
    if not window or not monitor.numeric(window.get("resetsAt")):
        return False
    reset, remaining = window["resetsAt"], window.get("remainingPercent")
    return (reset > now and now >= reset - 300 * 60 + 150 * 60 and
            monitor.numeric(remaining) and 0 <= remaining <= 100 and remaining >= 80)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".codex-usage-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def error_summary(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return f"Codex CLI failed with exit {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Codex CLI timed out at {exc.timeout}s; outcome unknown"
    return f"{type(exc).__name__}: {exc}"


def session_uuid(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        raise ValueError("Codex session ID must be an exact UUID")
    return value


def invoke(prompt, cwd, *, read_only=False, timeout=1800, model=None, effort=None, session_id=None, sandbox=None):
    if session_id:
        args = [CODEX, "exec", "resume", session_uuid(session_id), "--skip-git-repo-check", "--json"]
        if sandbox:
            if sandbox not in {"read-only", "workspace-write"}:
                raise ValueError("resume sandbox must be read-only or workspace-write")
            args += ["-c", f'sandbox_mode="{sandbox}"']
        if model:
            args += ["--model", model]
    else:
        args = [CODEX, "exec", "--ignore-user-config", "--skip-git-repo-check",
                "--sandbox", "read-only" if read_only else "workspace-write", "-C", str(cwd), "--json"]
        if model:
            args += ["--model", model]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    result = subprocess.run(args + [prompt], capture_output=True, text=True, timeout=timeout, check=True,
                            cwd=str(Path(cwd).resolve(strict=True)))
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        if event.get("type") in {"error", "turn.failed"}:
            raise RuntimeError(f"Codex reported {event.get('type')}: {event}")
    if not any(event.get("type") == "turn.completed" for event in events):
        raise RuntimeError("Codex CLI exited without a turn.completed JSON event; outcome uncertain")
    session_id = next((e.get("thread_id") for e in events
                      if e.get("type") == "thread.started" and e.get("thread_id")), None)
    return SimpleNamespace(stdout=result.stdout, returncode=result.returncode, session_id=session_id)


def prime(read=monitor.snapshot, state_path=STATE, now=None, sender=None):
    now = int(time.time()) if now is None else int(now)
    window = active_five_hour(read())
    if not window or not monitor.numeric(window.get("resetsAt")):
        return
    reset = int(window["resetsAt"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        previous_value = state.get("observedResetAt")
        previous = int(previous_value) if isinstance(previous_value, (int, str)) and str(previous_value).isdigit() else None

        # A future reset is a rollover only if we saw its predecessor expire,
        # and the new window is still completely unused. Otherwise establish a baseline.
        rollover = (reset > now and previous is not None and previous <= now and previous != reset
                    and window.get("usedPercent") == 0)
        if reset > now and not rollover:
            if state.get("observedResetAt") != str(reset):
                state["observedResetAt"] = str(reset)
                atomic_json(state_path, state)
            return

        claim_reset = previous if rollover else reset
        key = str(claim_reset)
        if state.get("primeAttemptedFor") == key:
            if state.get("observedResetAt") != str(reset):
                state["observedResetAt"] = str(reset)
                atomic_json(state_path, state)
            return
        # Claim before the network action: ambiguous outcomes must never auto-retry.
        state.update({"observedResetAt": str(reset), "primeAttemptedFor": key, "primeStatus": "running"})
        atomic_json(state_path, state)
        try:
            PRIME_CWD.mkdir(parents=True, exist_ok=True)
            (sender or (lambda: invoke("hello", PRIME_CWD, read_only=True,
                                      timeout=120, model="gpt-6-luna", effort="low")))()
            fresh = active_five_hour(read())
            if fresh and monitor.numeric(fresh.get("resetsAt")) and fresh["resetsAt"] > now and fresh["resetsAt"] != claim_reset:
                state.update({"primeStatus": "verified", "verifiedResetAt": fresh["resetsAt"]})
                print(f"Codex reset {key} expired; hello completed and new reset {fresh['resetsAt']} verified.")
            else:
                state["primeStatus"] = "uncertain; no automatic retry"
                atomic_json(state_path, state)
                raise RuntimeError("hello returned but a new future five-hour reset timestamp was not verified")
            atomic_json(state_path, state)
        except Exception as exc:
            state["primeStatus"] = "uncertain; no automatic retry"
            state["primeError"] = error_summary(exc)
            atomic_json(state_path, state)
            print(f"Codex hello after reset {key} uncertain; no retry: {error_summary(exc)}")
            raise


def task(read=monitor.snapshot, state_path=STATE, queue_path=QUEUE, now=None, runner=invoke):
    now = int(time.time()) if now is None else int(now)
    window = active_five_hour(read())
    if not eligible(window, now):
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("taskStatus") in {"running", "unknown; no automatic relaunch"}:
            return
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
        if not isinstance(queue, list):
            raise ValueError("tasks.json must contain a JSON array")
        ids = [entry.get("id") if isinstance(entry, dict) else None for entry in queue]
        if any(not isinstance(task_id, str) or not task_id.strip() for task_id in ids):
            raise ValueError("every queued task must have a non-empty string id")
        if len(ids) != len(set(ids)):
            raise ValueError("tasks.json contains duplicate task ids")
        if not queue:
            return
        item = queue[0]
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("task requires a non-empty string id")
        if item["id"] in {entry.get("id") for entry in state.get("taskHistory", [])}:
            raise ValueError(f"task id {item['id']!r} was already claimed")
        if not isinstance(item.get("cwd"), str) or not Path(item["cwd"]).is_absolute() or not Path(item["cwd"]).is_dir():
            raise ValueError(f"task {item.get('id')!r} needs an existing absolute cwd")
        if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
            raise ValueError(f"task {item['id']!r} needs a non-empty prompt")
        model = item.get("model", DEFAULT_TASK_MODEL)
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"task {item['id']!r} model must be a non-empty string")
        effort = item.get("reasoning_effort")
        if effort is not None and (not isinstance(effort, str) or effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}):
            raise ValueError(f"task {item['id']!r} reasoning_effort is unsupported")
        resume_id = session_uuid(item["session_id"]) if "session_id" in item else None
        cwd = Path(item["cwd"]).resolve(strict=True)
        history = state.setdefault("taskHistory", [])
        history.append({"id": item["id"], "resetAt": window["resetsAt"], "cwd": str(cwd), "status": "running"})
        state.pop("taskSessionId", None)
        state.update({"taskId": item["id"], "task": item, "taskStatus": "running", "taskCwd": str(cwd)})
        atomic_json(state_path, state)
        atomic_json(queue_path, queue[1:])
        try:
            options = {"model": model, "effort": effort}
            if resume_id:
                options.update(session_id=resume_id, sandbox="read-only")
            result = runner(item["prompt"], cwd, **options)
            session_id = getattr(result, "session_id", None)
            if not session_id:
                raise RuntimeError("Codex completed without returning a persistent session UUID")
            session_uuid(session_id)
            state["taskStatus"] = "completed"
            state["taskSessionId"] = session_id
            state["taskResult"] = getattr(result, "stdout", "")[-1000:]
            state["taskExitCode"] = getattr(result, "returncode", 0)
            history[-1].update({"status": "completed", "exitCode": state["taskExitCode"], "sessionId": session_id})
            state.pop("task", None)
            atomic_json(state_path, state)
            print(f"Codex task {item['id']} completed in {cwd} (exit {state['taskExitCode']}).")
        except Exception as exc:
            state["taskStatus"] = "unknown; no automatic relaunch"
            state["taskError"] = error_summary(exc)
            history[-1].update({"status": state["taskStatus"], "error": error_summary(exc)})
            atomic_json(state_path, state)
            print(f"Codex task {item['id']} outcome uncertain; claim and prompt retained for manual review: {error_summary(exc)}")
            raise


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prime", "task"}:
        raise SystemExit("usage: actions.py {prime|task}")
    (prime if sys.argv[1] == "prime" else task)()
