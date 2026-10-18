"""Parent-owned, bounded child tasks over the installed TUI stdio protocol.

No HTTP listener, arbitrary RPC passthrough, resume, or caller-supplied session IDs.
State is deliberately process-local and disappears with the owning process.
"""
import atexit
import contextvars
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import consult
from .policy import check

SCHEMA = {
    "name": "bot_task",
    "description": "Run and control bounded same-group child bot tasks owned by this parent session. spawn requires target/message; status/stop/steer require task_id; list takes no extra fields. No arbitrary session access. steer reports ambiguous acceptance, never claims guaranteed active-turn delivery (backend may queue a follow-up during compression). Results are retrieved with status; no automatic notifications.",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["spawn", "list", "status", "wait", "steer", "stop"]},
            "target": {"type": "string"}, "message": {"type": "string", "maxLength": 16000},
            "task_id": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600, "default": 300}},
        "required": ["action"]}}
_LOCK = threading.RLock()
_TASKS = {}


class Child:
    def __init__(self, argv, env, cwd, timeout):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=cwd,
            start_new_session=True, bufsize=1)
        self.lock = threading.RLock()
        self.pending = {}
        self.sid = None
        self.reply = ""
        self.outcome = None
        self.closed = False
        self.ready = threading.Event()
        self.done = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.timer = threading.Timer(timeout, self.expire)
        self.timer.daemon = True
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.timer.start()

    def send(self, frame):
        with self.lock:
            if self.closed:
                raise RuntimeError("child transport is closed")
            self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def rpc(self, method, params):
        key = uuid.uuid4().hex
        response = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[key] = response
        try:
            self.send({"jsonrpc": "2.0", "id": key, "method": method, "params": params})
            result = response.get(timeout=max(.01, min(30, self.deadline - time.monotonic())))
            if "error" in result:
                raise RuntimeError(str(result["error"]))
            return result.get("result", {})
        except queue.Empty as exc:
            raise RuntimeError("RPC timeout; processing outcome is unknown; do not resend") from exc
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def read(self):
        try:
            while True:
                line = self.proc.stdout.readline(1048577)
                if not line:
                    break
                if len(line) > 1048576:
                    raise RuntimeError("oversized child protocol frame")
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise RuntimeError("invalid child protocol frame")
                if "method" in frame and "id" in frame:
                    # Never auto-approve, answer secrets, or silently hang on an unsupported UI request.
                    self.send({"jsonrpc": "2.0", "id": frame["id"], "error": {
                        "code": -32601, "message": "Unattended bot task cannot answer interactive requests"}})
                elif frame.get("method") == "event":
                    event = frame.get("params", {})
                    if event.get("type") == "gateway.ready":
                        self.ready.set()
                    elif event.get("session_id") == self.sid and event.get("type") == "message.complete":
                        payload = event.get("payload", {})
                        with self.lock:
                            self.reply = str(payload.get("text", ""))[:32000]
                            self.outcome = payload.get("status", "completed")
                        self.done.set()
                else:
                    with self.lock:
                        waiter = self.pending.get(frame.get("id"))
                    if waiter:
                        waiter.put_nowait(frame)
        except Exception as exc:
            with self.lock:
                self.outcome = "transport_failed: " + str(exc)
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    if waiter.empty():
                        waiter.put_nowait({"error": "child transport exited; outcome unknown"})
            self.ready.set()
            self.done.set()

    def expire(self):
        with self.lock:
            if not self.closed:
                self.outcome = "timeout_unknown"
        self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.timer.cancel()
        consult._stop_group(self.proc)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass


def _start(task, target, text, timeout):
    child = None
    try:
        _, root = consult.identity()
        home = root if target == "default" else root / "profiles" / target
        env = dict(os.environ, HERMES_HOME=str(home), BOT_ACL_CONSULT_CHILD="1")
        for name in ("HERMES_TUI_ATTACH_URL", "HERMES_TUI_SIDECAR_URL", "HERMES_PROFILE", "HERMES_SESSION_ID"):
            env.pop(name, None)
        spec = importlib.util.find_spec("tui_gateway")
        if not spec or not spec.origin:
            raise RuntimeError("official TUI gateway is not installed")
        source = str(Path(spec.origin).parent.parent)
        env["PYTHONPATH"] = source
        with task["lock"]:
            if task.get("cancelled"):
                return
            child = Child([sys.executable, "-m", "tui_gateway.entry"], env, source, timeout)
            task["child"] = child
        if not child.ready.wait(min(30, timeout)) or child.proc.poll() is not None:
            raise RuntimeError("child gateway did not become ready")
        child.rpc("client.capabilities", {"server_requests": True})
        result = child.rpc("session.create", {"source": "bot_task", "title": "Bot task",
            "close_on_disconnect": True, "follow_profile_config": True})
        child.sid = result["session_id"]
        if not isinstance(child.sid, str) or not child.sid:
            raise RuntimeError("child returned invalid session ID")
        with task["lock"]:
            if task.get("cancelled"):
                child.close()
                return
            task["status"] = "running"
        child.rpc("prompt.submit", {"session_id": child.sid, "text": text})
    except Exception as exc:
        with task["lock"]:
            if not task.get("cancelled"):
                task.update(status="failed_unknown", error=str(exc))
        if child:
            child.close()


def _owner(kwargs):
    sid = kwargs.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("verified parent session_id is required from the tool runtime")
    return (str(consult.get_hermes_home()), sid)


def _snapshot(task):
    child = task["child"]
    return {"task_id": task["id"], "target": task["target"], "status": task["status"],
            "session_id": child.sid if child else None,
            "reply": child.reply if child else "", "error": task.get("error"),
            "finality_verified": False,
            "pending_steer_replay": "backend-owned; no client resend; exact-once not observable via this protocol",
            "restart_recovery": False,
            "deadline_seconds": max(0, int(child.deadline - time.monotonic())) if child else 0}


def handle(domains, args, **kwargs):
    try:
        if os.environ.get(consult.DEPTH_ENV) == "1":
            raise ValueError("nested child tasks are disabled")
        owner = _owner(kwargs)
        action = args.get("action")
        allowed = {"spawn": {"action", "target", "message", "timeout_seconds"},
                   "list": {"action"}, "status": {"action", "task_id"},
                   "wait": {"action", "task_id"},
                   "stop": {"action", "task_id"}, "steer": {"action", "task_id", "message"}}
        if action not in allowed or set(args) - allowed[action]:
            raise ValueError("unknown action or unsupported action arguments")
        if action in ("spawn", "steer"):
            text = args.get("message")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ValueError("message must contain 1-16000 characters")
        with _LOCK:
            if action == "list":
                return json.dumps({"tasks": [_snapshot(t) for t in _TASKS.values()
                    if t["owner"] == owner and check(domains, "message_agent", {"target": t["target"]}) is None]})
            if action == "spawn":
                timeout = args.get("timeout_seconds", 300)
                if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 600:
                    raise ValueError("timeout_seconds must be an integer between 30 and 600")
                denied = check(domains, "message_agent", args)
                if denied:
                    return json.dumps({"status": "blocked", "error": denied["message"]})
                active = [t for t in _TASKS.values() if t["status"] == "starting" or (t["child"] and not t["child"].closed)]
                if len(active) >= 16 or sum(t["owner"] == owner for t in active) >= 4:
                    raise ValueError("active child task limit reached (4 per parent, 16 per process)")
                # Bounded retained history. Never evict a live task.
                completed = [k for k,t in _TASKS.items() if t["status"] != "starting" and (t["child"] is None or t["child"].closed)]
                for key in completed[:-64]:
                    del _TASKS[key]
                target = args["target"].strip().removeprefix("@")
                target = "default" if target == "hermes" else target
                task = {"id": uuid.uuid4().hex, "owner": owner, "target": target,
                        "status": "starting", "child": None, "lock": threading.RLock()}
                _TASKS[task["id"]] = task
            else:
                task = _TASKS.get(args.get("task_id"))
                if not task or task["owner"] != owner:
                    raise ValueError("unknown task owned by this parent")
                # Recheck the live ACL before any child data or operation is exposed.
                denied = check(domains, "message_agent", {"target": task["target"]})
                if denied:
                    return json.dumps({"status": "blocked", "error": denied["message"]})
        with task["lock"]:
            if action == "spawn":
                context = contextvars.copy_context()
                threading.Thread(target=context.run,
                    args=(_start, task, target, text, timeout), daemon=True,
                    name="bot-task-start-" + task["id"][:8]).start()
            else:
                child = task["child"]
                if child and child.closed and task["status"] == "running":
                    task["status"] = child.outcome or "transport_closed_unknown"
                if action == "stop":
                    task["cancelled"] = True
                    task["status"] = "stopped_unknown"
                    if child:
                        # Kill this dedicated child, including backend-owned pending
                        # follow-ups. Do not race interrupt RPC against auto replay.
                        child.close()
                elif action == "steer":
                    if not child or child.closed or task["status"] != "running":
                        raise ValueError("task is not running")
                    result = child.rpc("session.steer", {"session_id": child.sid, "text": text})
                    return json.dumps({"task_id": task["id"], "status": "accepted_unconfirmed" if result.get("status") == "queued" else result.get("status", "unknown"),
                        "backend_result": result, "active_turn_delivery_verified": False,
                        "explanation": "Backend queued is ambiguous: live tool-result injection OR follow-up during compression. No automatic resend or fallback."})
                elif action in ("status", "wait") and child and child.sid and not child.closed:
                    if action == "wait":
                        child.done.wait(2)
                    state = child.rpc("session.activate", {"session_id": child.sid, "omit_messages": True})
                    if child.done.is_set() and state.get("running") is False:
                        task["status"] = "completed" if child.outcome == "completed" else "failed"
                        # message.complete precedes backend post-turn follow-ups.
                        # Keep the child alive: closing here can kill pending_steer
                        # replay in the backend's turn-release window. Timer/stop
                        # bounds its lifetime. Later status can observe another turn.
                    elif state.get("running") is True:
                        task["status"] = "running"
                        child.done.clear()
            return json.dumps(_snapshot(task), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def shutdown():
    with _LOCK:
        children = [t["child"] for t in _TASKS.values() if t["child"]]
    for child in children:
        child.close()


atexit.register(shutdown)


def register_tasks(ctx, domains):
    ctx.register_tool("bot_task", consult.TOOLSET, SCHEMA,
        lambda args, **kw: handle(domains, args, **kw), check_fn=consult.available,
        description="Manage parent-owned bot tasks", emoji="🧩")
