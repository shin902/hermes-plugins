"""Parent-owned asynchronous bot tasks over the public Hermes CLI.

Each task turn is one ``hermes -p <target> chat --query-file - --format
stream-json`` subprocess. There is no TUI JSON-RPC transport, HTTP listener,
arbitrary RPC passthrough, or caller-supplied runtime session ID. A task may
open a fresh child session or resume one exact target-owned stored session.

Steering interrupts the active CLI process group with SIGTERM, waits for the
predecessor to exit, then resumes the same returned session ID in the same
target profile with only the new guidance. Stop wins races and blocks automatic
resume. Forced shutdown or a missing session ID also blocks resume. State is
process-local and disappears with the owning process.
"""
import atexit
import contextvars
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import ask_bot
from .policy import check

PROMPT = (
    "Run parent-owned child bot tasks with bot_task from ordinary sessions. "
    "Use list_bot_peers to discover allowed teammates. spawn requires target and "
    "message. Choose mode=new_session for an isolated child (default) or "
    "mode=resume with target and an exact target-owned session_id. bot_task never "
    "substitutes another mode. There is no wall-clock timer. Use status or wait to "
    "retrieve a result. steer interrupts the active CLI turn with SIGTERM, waits "
    "for it to exit, then resumes the same returned session with only the new "
    "guidance; the original prompt is never resent. stop wins races. There is no "
    "automatic retry, mode switch, or resend."
)
SCHEMA = {
    "name": "bot_task",
    "description": "Run and control same-group child bot tasks owned by this parent session. spawn requires target/message and chooses mode=new_session (default) or resume with an exact target-owned session_id. There is no wall-clock timer. status/wait/stop/steer require task_id; list takes no extra fields. steer interrupts the active public CLI turn with SIGTERM, waits for the predecessor to exit, then resumes the same returned session with only the new guidance; it never resends the original prompt. stop wins races and blocks automatic resume. Results are retrieved with status or wait; no automatic notifications. No automatic retry, mode switch, or resend.",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["spawn", "list", "status", "wait", "steer", "stop"]},
            "target": {"type": "string"},
            "message": {"type": "string", "maxLength": 16000},
            "task_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["new_session", "resume"], "default": "new_session"},
            "session_id": {"type": "string", "description": "Only with mode=resume: an exact stored session ID in the target profile."},
        },
        "required": ["action"]}}
_LOCK = threading.RLock()
_TASKS = {}

SHUTDOWN_GRACE = 5.0
KILL_GRACE = 5.0
POLL_INTERVAL = 0.05
MAX_TASKS = 16
MAX_TASKS_PER_OWNER = 4
RETAIN_COMPLETED = 64


class Turn:
    """One public-CLI turn. Exactly one turn may own a task's writer at a time."""

    def __init__(self, argv, payload, env, cwd, scratch, target, mode,
                 requested_session_id=None):
        self.argv = argv
        self.payload = payload
        self.env = env
        self.cwd = str(cwd)
        self.scratch = Path(scratch)
        self.target = target
        self.mode = mode
        self.requested_session_id = requested_session_id
        self.lock = threading.RLock()
        self.proc = None
        self.session_id = None
        self.reply = ""
        self.partial_reply = None
        self.error = None
        self.outcome = None
        self.exit_code = None
        self.forced = False
        self.interrupted = False
        self.signaled = False
        self.done = threading.Event()
        self.started = threading.Event()
        self._cancel_requested = threading.Event()
        self._offset = 0
        self._pending = b""
        self._dropping = False
        self._output = tempfile.TemporaryFile(dir=self.scratch)
        self._errors = tempfile.TemporaryFile(dir=self.scratch)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="bot-task-turn")
        self._thread.start()

    def _collect(self):
        try:
            while True:
                chunk = os.pread(self._output.fileno(), 65536, self._offset)
                if not chunk:
                    break
                self._offset += len(chunk)
                for part in chunk.splitlines(keepends=True):
                    ended = part.endswith(b"\n")
                    if not self._dropping:
                        self._pending += part
                        if len(self._pending) > 65536:
                            self._pending, self._dropping = b"", True
                        elif ended:
                            try:
                                record = json.loads(self._pending)
                            except (ValueError, TypeError):
                                record = None
                            if (isinstance(record, dict) and record.get("type") == "system"
                                    and record.get("subtype") == "init"):
                                sid = ask_bot._valid_id(record.get("session_id"))
                                if sid:
                                    with self.lock:
                                        if self.session_id is None:
                                            self.session_id = sid
                    if ended:
                        self._pending, self._dropping = b"", False
        except (OSError, ValueError):
            pass

    def _finish(self):
        try:
            output = ask_bot._tail(self._output)
            errors = ask_bot._tail(self._errors, 8192)
        except Exception as exc:
            output, errors = "", "transport_failed: " + str(exc)
        parsed = ask_bot._outcome(self.exit_code, output, errors, False,
                                  self.target, self.mode)
        with self.lock:
            # A terminal result may carry a compression-continuation ID that
            # differs from the turn's init ID. Prefer the parsed terminal ID;
            # fall back to the init ID, then the exact requested resume ID.
            self.session_id = (parsed.get("session_id") or self.session_id
                               or (self.requested_session_id if self.mode == "resume" else None))
            self.reply = parsed.get("reply", "") if isinstance(parsed.get("reply"), str) else ""
            self.partial_reply = parsed.get("partial_reply")
            self.error = parsed.get("error")
            self.outcome = parsed.get("status", "failed")
            if self.interrupted:
                self.outcome = "forced_stop_unknown" if self.forced else "interrupted"
                if self.forced:
                    self.error = ("CLI turn required SIGKILL; persistence is unknown and "
                                  "automatic resume is blocked.")
                elif not self.error:
                    self.error = "CLI turn was interrupted"
                if self.partial_reply is None and self.reply:
                    self.partial_reply = self.reply
                self.reply = ""
        try:
            self._output.close()
        except Exception:
            pass
        try:
            self._errors.close()
        except Exception:
            pass
        self.done.set()

    def _run(self):
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=self._output,
                stderr=self._errors, env=self.env, cwd=self.cwd,
                start_new_session=True)
            self.started.set()
            if self._cancel_requested.is_set():
                ask_bot._signal_group(self.proc, signal.SIGTERM)
                # A stop that won startup must not hand the child the task the
                # caller already cancelled. Close stdin without the payload so
                # the child sees EOF, then let the normal loop wait and reap it.
                try:
                    self.proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            else:
                try:
                    self.proc.stdin.write(self.payload.encode("utf-8"))
                    self.proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            while True:
                self._collect()
                if self.proc.poll() is not None:
                    break
                time.sleep(POLL_INTERVAL)
            self.exit_code = self.proc.returncode
            self._collect()
            self._finish()
        except Exception as exc:
            with self.lock:
                self.error = "transport_failed: " + str(exc)
                self.outcome = "failed"
            for stream in (self._output, self._errors):
                try:
                    stream.close()
                except Exception:
                    pass
            self.started.set()
            self.done.set()

    def interrupt(self, grace=None, kill_grace=None):
        """Claim and stop this turn. Return None if completion already won."""
        grace = SHUTDOWN_GRACE if grace is None else grace
        kill_grace = KILL_GRACE if kill_grace is None else kill_grace
        self._cancel_requested.set()
        with self.lock:
            if self.done.is_set() or self.outcome is not None:
                return None
            if self.proc is not None and self.proc.poll() is not None:
                # The process exited; let the turn thread publish completion.
                self.done.wait(kill_grace)
                return None
            self.interrupted = True
        if not self.started.wait(grace):
            self.done.wait(kill_grace)
            with self.lock:
                if self.forced:
                    return "forced"
                if not self.done.is_set():
                    return None
                if not self.signaled and self.outcome == "failed":
                    return None
                return "graceful"
        proc = self.proc
        if proc is None:
            self.done.wait(kill_grace)
            with self.lock:
                return "forced" if self.forced else None
        if proc.poll() is None:
            self.signaled = True
            ask_bot._signal_group(proc, signal.SIGTERM)
            if not self.done.wait(grace):
                with self.lock:
                    self.forced = True
                ask_bot._signal_group(proc, signal.SIGKILL)
                self.done.wait(kill_grace)
        else:
            self.done.wait(kill_grace)
        with self.lock:
            if self.forced:
                return "forced"
            if not self.done.is_set():
                return None
            if not self.signaled and self.outcome == "failed":
                # Startup failed before we could signal anything; this is not an
                # interruption and must not trigger an automatic resume.
                return None
            return "graceful"


def _target_home(root, target):
    return Path(root) if target == "default" else Path(root) / "profiles" / target


def _source_and_root():
    return ask_bot.identity()


def _launch(task, target, text, mode, session_id, source, reason):
    with task["lock"]:
        if task.get("cancelled") or task["stop"].is_set():
            return None
    executable = ask_bot._executable()
    if not executable:
        raise RuntimeError("Hermes CLI is not available on this host")
    _, root = _source_and_root()
    target_home = _target_home(root, target)
    env, scratch = ask_bot._child_env(target_home)
    argv = ask_bot._cli_argv(executable, target, mode, session_id, "bot_task")
    payload = ask_bot._payload(source, text, task=(reason != "steer"),
                               steer=(reason == "steer"))
    turn = Turn(argv, payload, env, Path.home(), scratch, target, mode, session_id)
    with task["lock"]:
        if task.get("cancelled") or task["stop"].is_set():
            turn.interrupt(grace=1.0, kill_grace=2.0)
            return None
        task["turn"] = turn
        task["turns"] = task.get("turns", 0) + 1
        task["status"] = "running"
        task["error"] = None
        task["reply"] = ""
        task["partial_reply"] = None
        task["forced_kill"] = False
        task["interrupted"] = False
        if mode == "resume":
            task["session_id"] = session_id
    threading.Thread(target=_watch, args=(task, turn), daemon=True,
                     name="bot-task-watch-" + task["id"][:8]).start()
    return turn


def _watch(task, turn):
    turn.done.wait()
    with task["lock"]:
        if task.get("turn") is not turn:
            return
        if task.get("status") not in ("starting", "running", "interrupting"):
            # A control action (stop/steer) already recorded the task-level
            # outcome for this turn; do not let the settled predecessor
            # overwrite its actionable status or error.
            return
        if task.get("cancelled") and turn.interrupted:
            task["status"] = "stopped_unknown" if turn.forced else "stopped"
        else:
            task["status"] = turn.outcome or "failed"
        task["session_id"] = turn.session_id or task.get("session_id")
        task["error"] = turn.error
        task["reply"] = turn.reply
        task["partial_reply"] = turn.partial_reply
        task["forced_kill"] = turn.forced
        task["interrupted"] = turn.interrupted


def _start(task, target, text, mode, requested_session_id):
    try:
        source, root = _source_and_root()
        if mode == "resume":
            ask_bot._require_exact_session_id(_target_home(root, target),
                                              requested_session_id)
        _launch(task, target, text, mode, requested_session_id, source, "spawn")
    except Exception as exc:
        with task["lock"]:
            if not task.get("cancelled"):
                task["status"] = "failed_unknown"
                task["error"] = str(exc)
                task["next_step"] = ("Report the failure. Do not automatically retry, "
                                     "resend, or switch modes.")


def _owner(kwargs):
    sid = kwargs.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("verified parent session_id is required from the tool runtime")
    return (str(ask_bot.get_hermes_home()), sid)


def _refresh(task):
    turn = task.get("turn")
    if not turn or not turn.done.is_set() or task.get("turn") is not turn:
        return
    if task.get("status") not in ("starting", "running", "interrupting"):
        # A settled task-level outcome is authoritative for this turn.
        return
    if task.get("cancelled") and turn.interrupted:
        task["status"] = "stopped_unknown" if turn.forced else "stopped"
    else:
        task["status"] = turn.outcome or "failed"
    task["session_id"] = turn.session_id or task.get("session_id")
    task["error"] = turn.error
    task["reply"] = turn.reply
    task["partial_reply"] = turn.partial_reply
    task["forced_kill"] = turn.forced
    task["interrupted"] = turn.interrupted


def _snapshot(task):
    turn = task.get("turn")
    # A task-level error (missing session ID, revoked ACL, replacement launch
    # failure) describes the action that just failed. Prefer it over the
    # predecessor turn's interruption error, which would mask the actionable
    # cause; fall back to the turn error until the turn is synchronized.
    error = task.get("error")
    if error is None and turn is not None:
        error = turn.error
    snapshot = {
        "task_id": task["id"], "target": task["target"],
        "mode": task.get("mode", "new_session"), "status": task["status"],
        "session_id": task.get("session_id") or (turn.session_id if turn else None),
        "requested_session_id": task.get("requested_session_id"),
        "reply": (turn.reply if turn else task.get("reply", "")) or "",
        "error": error,
        "next_step": task.get("next_step"),
        "turn": task.get("turns", 0),
        "steer_count": task.get("steer_count", 0),
        "steer_status": task.get("steer_status"),
        "stop_status": task.get("stop_status"),
        "interrupted": bool(turn.interrupted) if turn else bool(task.get("interrupted")),
        "forced_kill": bool(turn.forced) if turn else bool(task.get("forced_kill")),
        "original_prompt_resent": False,
        "finality_verified": bool(turn and turn.done.is_set()
                                  and not turn.forced and not turn.interrupted),
        "transport": "public-cli-stream-json",
        "resident": False,
    }
    partial = turn.partial_reply if turn and turn.partial_reply is not None else task.get("partial_reply")
    if partial is not None:
        snapshot["partial_reply"] = partial
    return snapshot


def _steer_ended(task):
    """Record that steer found no live writer, preserving the settled outcome."""
    _refresh(task)
    if task.get("status") == "completed":
        task["steer_status"] = "not_applied_completed"
        task["error"] = "The active turn completed before steer; no guidance was sent."
    else:
        task["steer_status"] = "not_applied"
        # Preserve the settled turn's real failure (for example a quota error);
        # the steer status already records that no guidance was sent.
        if not task.get("error"):
            task["error"] = ("The active turn had already ended before steer; "
                             "no guidance was sent.")
    return _snapshot(task)


def _steer(task, domains, text):
    if task.get("cancelled") or task["stop"].is_set():
        turn = task.get("turn")
        if turn is not None and turn.done.is_set() and not turn.interrupted:
            return _steer_ended(task)
        task["status"] = "stopped"
        task["steer_status"] = "stopped"
        task["error"] = "Stop won the race; guidance was not resumed."
        return _snapshot(task)
    turn = task.get("turn")
    if turn is None or turn.done.is_set() or task["status"] not in ("starting", "running", "interrupting"):
        if turn is not None and turn.done.is_set():
            return _steer_ended(task)
        task["steer_status"] = "not_applied"
        task["error"] = "Task has no active turn; no guidance was sent."
        return _snapshot(task)
    session_id = turn.session_id or task.get("session_id")
    task["status"] = "interrupting"
    result = turn.interrupt()
    session_id = turn.session_id or session_id or task.get("session_id")
    if task.get("cancelled") or task["stop"].is_set():
        task["forced_kill"] = turn.forced
        if turn.done.is_set() and not turn.interrupted:
            # Stop won, but the turn had already settled on its own: keep the
            # truthful completed/failed outcome instead of rewriting it.
            return _steer_ended(task)
        task["status"] = "stopped_unknown" if turn.forced else "stopped"
        task["steer_status"] = "stopped"
        task["error"] = "Stop won the race; guidance was not resumed."
        return _snapshot(task)
    if result is None:
        return _steer_ended(task)
    if turn.forced:
        task["status"] = "steer_blocked"
        task["steer_status"] = "blocked_forced_kill"
        task["error"] = ("The active CLI turn required SIGKILL; persistence is uncertain, "
                         "so automatic resume was blocked.")
        task["next_step"] = ("Do not resend automatically. Inspect the target session "
                             "before starting a new task.")
        task["forced_kill"] = True
        return _snapshot(task)
    if not session_id:
        task["status"] = "steer_blocked"
        task["steer_status"] = "blocked_no_session_id"
        task["error"] = ("The interrupted CLI turn returned no session_id; automatic "
                         "resume was blocked.")
        task["next_step"] = ("Do not resend automatically. Start a new task only with "
                             "explicit user direction.")
        return _snapshot(task)
    denied = check(domains, "message_agent", {"target": task["target"]})
    if denied:
        task["status"] = "blocked"
        task["steer_status"] = "blocked_acl"
        task["error"] = denied["message"]
        return _snapshot(task)
    try:
        source, _ = _source_and_root()
        launched = _launch(task, task["target"], text, "resume", session_id,
                           source, "steer")
    except Exception as exc:
        task["status"] = "steer_blocked"
        task["steer_status"] = "blocked_launch_failed"
        task["error"] = str(exc)
        return _snapshot(task)
    if launched is None:
        task["status"] = "stopped"
        task["steer_status"] = "stopped"
        task["error"] = "Stop won the race; guidance was not resumed."
        return _snapshot(task)
    task["steer_count"] = task.get("steer_count", 0) + 1
    task["steer_status"] = "resumed"
    task["next_step"] = ("New guidance was submitted on a resumed CLI turn; the "
                         "original prompt was not resent.")
    return _snapshot(task)


def _is_active(task):
    if task.get("cancelled"):
        return False
    # A steer handoff remains active after its predecessor turn settles: the
    # replacement turn is launched under the same task record. Counting it as
    # inactive here would let a concurrent spawn exceed the owner/process limits
    # and could evict the handoff task from _TASKS.
    if task["status"] == "interrupting":
        return True
    turn = task.get("turn")
    if turn is not None and turn.done.is_set():
        return False
    return task["status"] in ("starting", "running")


def handle(domains, args, **kwargs):
    try:
        if os.environ.get(ask_bot.DEPTH_ENV) == "1":
            raise ValueError("nested child tasks are disabled")
        owner = _owner(kwargs)
        if "timeout_seconds" in args:
            raise ValueError("timeout_seconds is not supported; bot tasks are always unlimited")
        action = args.get("action")
        allowed = {"spawn": {"action", "target", "message", "mode", "session_id"},
                   "list": {"action"}, "status": {"action", "task_id"},
                   "wait": {"action", "task_id"},
                   "stop": {"action", "task_id"}, "steer": {"action", "task_id", "message"}}
        if action not in allowed or set(args) - allowed[action]:
            raise ValueError("unknown action or unsupported action arguments")
        if action in ("spawn", "steer"):
            text = args.get("message")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ValueError("message must contain 1-16000 characters")
        if action == "spawn":
            mode = args.get("mode", "new_session")
            if mode not in ("new_session", "resume"):
                raise ValueError("mode must be new_session or resume")
            requested_session_id = args.get("session_id")
            if mode == "resume":
                if not ask_bot._valid_id(requested_session_id) or requested_session_id.lower() == "latest":
                    raise ValueError("resume requires an exact session_id, not a title or latest")
            elif "session_id" in args:
                raise ValueError("session_id is only allowed with mode=resume")
        with _LOCK:
            if action == "list":
                tasks = []
                for task in _TASKS.values():
                    if task["owner"] != owner:
                        continue
                    if check(domains, "message_agent", {"target": task["target"]}) is not None:
                        continue
                    with task["lock"]:
                        _refresh(task)
                        tasks.append(_snapshot(task))
                return json.dumps({"tasks": tasks})
            if action == "spawn":
                denied = check(domains, "message_agent", args)
                if denied:
                    return json.dumps({"status": "blocked", "error": denied["message"]})
                # Normalize only after the live ACL has accepted the canonical target.
                target = args["target"].strip().removeprefix("@")
                target = "default" if target == "hermes" else target
                active = [t for t in _TASKS.values() if _is_active(t)]
                if (len(active) >= MAX_TASKS
                        or sum(t["owner"] == owner for t in active) >= MAX_TASKS_PER_OWNER):
                    raise ValueError("active child task limit reached (4 per parent, 16 per process)")
                completed = [k for k, t in _TASKS.items()
                             if not _is_active(t) and (t.get("turn") is None
                                                       or t["turn"].done.is_set())]
                for key in completed[:-RETAIN_COMPLETED]:
                    del _TASKS[key]
                task = {"id": uuid.uuid4().hex, "owner": owner, "target": target,
                        "mode": mode,
                        "requested_session_id": requested_session_id if mode == "resume" else None,
                        "status": "starting", "turn": None, "lock": threading.RLock(),
                        "stop": threading.Event(), "cancelled": False,
                        "turns": 0, "steer_count": 0, "steer_status": None,
                        "session_id": requested_session_id if mode == "resume" else None}
                _TASKS[task["id"]] = task
            else:
                task = _TASKS.get(args.get("task_id"))
                if not task or task["owner"] != owner:
                    raise ValueError("unknown task owned by this parent")
                # Recheck the live ACL before any child data or operation is exposed.
                denied = check(domains, "message_agent", {"target": task["target"]})
                if denied:
                    return json.dumps({"status": "blocked", "error": denied["message"]})
                if action == "stop":
                    # Set the race flag before waiting for an in-flight steer.
                    # Marking the task cancelled happens only after we know a
                    # live turn was actually interrupted.
                    task["stop"].set()
        if action == "wait":
            turn = task.get("turn")
            if turn is not None:
                turn.done.wait(2)
        with task["lock"]:
            if action == "spawn":
                context = contextvars.copy_context()
                threading.Thread(target=context.run,
                    args=(_start, task, target, text, mode, requested_session_id),
                    daemon=True, name="bot-task-start-" + task["id"][:8]).start()
            else:
                _refresh(task)
                if action == "stop":
                    turn = task.get("turn")
                    if turn is None:
                        if task["status"] == "starting":
                            # No writer exists yet; cancel the pending launch.
                            task["cancelled"] = True
                            task["status"] = "stopped"
                        else:
                            # Startup already failed (for example a missing
                            # session ID). Preserve that settled error.
                            task["stop_status"] = "already_finished"
                    elif turn.done.is_set() and not turn.interrupted:
                        # Stop arrived after the turn had already settled. Do not
                        # rewrite a completed/failed outcome as stopped.
                        task["stop_status"] = "already_finished"
                        task["status"] = turn.outcome or "failed"
                    else:
                        task["cancelled"] = True
                        result = turn.interrupt()
                        if result == "forced":
                            task["status"] = "stopped_unknown"
                            task["error"] = ("CLI turn required SIGKILL; persistence is "
                                             "unknown and automatic resume is blocked.")
                            task["next_step"] = ("Do not resume automatically; inspect the "
                                                 "target session.")
                        elif result == "graceful":
                            task["status"] = "stopped"
                            task["error"] = "Task was stopped; no automatic resume."
                        else:
                            _refresh(task)
                        task["forced_kill"] = turn.forced
                        task["interrupted"] = turn.interrupted
                elif action == "steer":
                    return json.dumps(_steer(task, domains, text), ensure_ascii=False)
                elif action in ("status", "wait"):
                    _refresh(task)
            return json.dumps(_snapshot(task), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def shutdown():
    with _LOCK:
        tasks = list(_TASKS.values())
    for task in tasks:
        turn = task.get("turn")
        if turn is not None and not turn.done.is_set():
            turn.interrupt(grace=1.0, kill_grace=2.0)


atexit.register(shutdown)


def register(ctx, domains):
    ctx.register_tool("bot_task", ask_bot.TOOLSET, SCHEMA,
        lambda args, **kw: handle(domains, args, **kw), check_fn=ask_bot.available,
        description="Manage parent-owned bot tasks", emoji="🧩")


def register_prompt(ctx):
    ctx.register_system_prompt_section("bot-acl.tasks", PROMPT, position="after_memory")
