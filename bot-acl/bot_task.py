"""Parent-owned, bounded child tasks over the installed TUI stdio protocol.

No HTTP listener, arbitrary RPC passthrough, or caller-supplied runtime session
IDs. A task may open a fresh child session or resume one exact target-owned
stored session. The target-scoped ``session.resume`` RPC validates that the
stored session exists in that profile and owns it; this module never reads the
private session store. State is deliberately process-local and disappears with
the owning process.
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
    "retrieve a result, steer with the understanding that delivery is ambiguous, "
    "and stop to kill the child. There is no automatic retry, mode switch, or resend."
)
SCHEMA = {
    "name": "bot_task",
    "description": "Run and control same-group child bot tasks owned by this parent session. spawn requires target/message and chooses mode=new_session (default) or resume with an exact target-owned session_id. There is no wall-clock timer. status/wait/stop/steer require task_id; list takes no extra fields. Results are retrieved with status or wait; no automatic notifications. steer reports ambiguous acceptance, never claims guaranteed active-turn delivery. No automatic retry, mode switch, or resend.",
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["spawn", "list", "status", "wait", "steer", "stop"]},
            "target": {"type": "string"},
            "message": {"type": "string", "maxLength": 16000},
            "task_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["new_session", "resume"], "default": "new_session"},
            "session_id": {"type": "string", "description": "Only with mode=resume: an exact stored session ID in the target profile."}},
        "required": ["action"]}}
_LOCK = threading.RLock()
_TASKS = {}


class RPCError(RuntimeError):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class Child:
    def __init__(self, argv, env, cwd):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=cwd,
            start_new_session=True, bufsize=1)
        self.lock = threading.RLock()
        self.pending = {}
        self.sid = None
        self.session_key = None
        self.reply = ""
        self.error = None
        self.outcome = None
        self.closed = False
        self.ready = threading.Event()
        self.done = threading.Event()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

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
            result = response.get(timeout=30)
            if "error" in result:
                error = result["error"]
                if isinstance(error, dict):
                    raise RPCError(str(error.get("message") or error),
                                   code=error.get("code"), data=error.get("data"))
                raise RPCError(str(error))
            return result.get("result", {})
        except queue.Empty as exc:
            raise RPCError("RPC timeout; processing outcome is unknown; do not resend") from exc
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
                    elif event.get("session_id") == self.sid and event.get("type") == "error":
                        message = event.get("payload", {}).get("message")
                        with self.lock:
                            if message:
                                self.error = str(message)
                            if self.outcome is None:
                                self.outcome = "failed"
                        self.done.set()
                    elif event.get("session_id") == self.sid and event.get("type") == "message.complete":
                        payload = event.get("payload", {})
                        status = payload.get("status")
                        with self.lock:
                            self.reply = str(payload.get("text", ""))[:32000]
                            if status == "complete":
                                self.outcome = "completed"
                                self.error = None
                            elif status == "interrupted":
                                self.outcome = "failed"
                                self.error = "recipient turn was interrupted"
                            else:
                                self.outcome = "failed"
                                self.error = str(payload.get("error") or payload.get("text") or "recipient turn failed")
                        self.done.set()
                else:
                    with self.lock:
                        waiter = self.pending.get(frame.get("id"))
                    if waiter:
                        waiter.put_nowait(frame)
        except Exception as exc:
            with self.lock:
                self.outcome = "failed"
                self.error = "transport_failed: " + str(exc)
        finally:
            with self.lock:
                for waiter in self.pending.values():
                    if waiter.empty():
                        waiter.put_nowait({"error": {"message": "child transport exited; outcome unknown"}})
            self.ready.set()
            self.done.set()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        ask_bot._stop_group(self.proc)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass


def _ownership_refusal(exc):
    if not isinstance(exc, RPCError):
        return False
    reason = exc.data.get("reason") if isinstance(exc.data, dict) else None
    return exc.code in (4009, 4090) or reason == "SESSION_NOT_OWNED" or "SESSION_NOT_OWNED" in str(exc)


def _open_session(child, mode, requested_session_id):
    if mode == "new_session":
        return child.rpc("session.create", {"source": "bot_task", "title": "Bot task",
            "close_on_disconnect": True, "follow_profile_config": True})
    return child.rpc("session.resume", {"session_id": requested_session_id,
        "omit_messages": True, "close_on_disconnect": True})


def _start(task, target, text, mode, requested_session_id):
    child = None
    try:
        _, root = ask_bot.identity()
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
            child = Child([sys.executable, "-m", "tui_gateway.entry"], env, source)
            task["child"] = child
        if not child.ready.wait(30) or child.proc.poll() is not None:
            raise RuntimeError("child gateway did not become ready")
        child.rpc("client.capabilities", {"server_requests": True})
        result = _open_session(child, mode, requested_session_id)
        child.sid = ask_bot._valid_id(result.get("session_id")) if isinstance(result, dict) else None
        if not child.sid:
            raise RuntimeError("child returned invalid runtime session ID")
        child.session_key = ask_bot._valid_id(
            result.get("stored_session_id") or result.get("resumed") or result.get("session_key")
        ) or (requested_session_id if mode == "resume" else None)
        with task["lock"]:
            if task.get("cancelled"):
                child.close()
                return
        child.rpc("prompt.submit", {"session_id": child.sid, "text": text})
        with task["lock"]:
            if task.get("cancelled"):
                child.close()
                return
            task["status"] = "running"
    except Exception as exc:
        with task["lock"]:
            if not task.get("cancelled"):
                if _ownership_refusal(exc):
                    task.update(status="target_busy", error=str(exc),
                                next_step="Report the recipient session as busy. Do not automatically retry, resend, or switch modes.")
                else:
                    task.update(status="failed_unknown", error=str(exc))
        if child:
            child.close()


def _owner(kwargs):
    sid = kwargs.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("verified parent session_id is required from the tool runtime")
    return (str(ask_bot.get_hermes_home()), sid)


def _snapshot(task):
    child = task["child"]
    return {"task_id": task["id"], "target": task["target"], "mode": task.get("mode", "new_session"),
            "status": task["status"],
            "session_id": child.sid if child else None,
            "stored_session_id": child.session_key if child else None,
            "requested_session_id": task.get("requested_session_id"),
            "reply": child.reply if child else "",
            "error": (child.error if child and child.error else None) or task.get("error"),
            "next_step": task.get("next_step"),
            "finality_verified": False,
            "pending_steer_replay": "backend-owned; no client resend; exact-once not observable via this protocol",
            "restart_recovery": False}


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
                return json.dumps({"tasks": [_snapshot(t) for t in _TASKS.values()
                    if t["owner"] == owner and check(domains, "message_agent", {"target": t["target"]}) is None]})
            if action == "spawn":
                denied = check(domains, "message_agent", args)
                if denied:
                    return json.dumps({"status": "blocked", "error": denied["message"]})
                # Normalize only after the live ACL has accepted the canonical target.
                target = args["target"].strip().removeprefix("@")
                target = "default" if target == "hermes" else target
                # Existence and ownership are validated by the target-scoped
                # session.resume RPC in the child, not by reading its state.db.
                active = [t for t in _TASKS.values() if t["status"] == "starting" or (t["child"] and not t["child"].closed)]
                if len(active) >= 16 or sum(t["owner"] == owner for t in active) >= 4:
                    raise ValueError("active child task limit reached (4 per parent, 16 per process)")
                # Bounded retained history. Never evict a live task.
                completed = [k for k,t in _TASKS.items() if t["status"] != "starting" and (t["child"] is None or t["child"].closed)]
                for key in completed[:-64]:
                    del _TASKS[key]
                task = {"id": uuid.uuid4().hex, "owner": owner, "target": target,
                        "mode": mode, "requested_session_id": requested_session_id if mode == "resume" else None,
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
                    args=(_start, task, target, text, mode, requested_session_id), daemon=True,
                    name="bot-task-start-" + task["id"][:8]).start()
            else:
                child = task["child"]
                if child and child.closed and task["status"] in ("running", "starting"):
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
                        task["status"] = child.outcome or "failed"
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


def register(ctx, domains):
    ctx.register_tool("bot_task", ask_bot.TOOLSET, SCHEMA,
        lambda args, **kw: handle(domains, args, **kw), check_fn=ask_bot.available,
        description="Manage parent-owned bot tasks", emoji="🧩")


def register_prompt(ctx):
    ctx.register_system_prompt_section("bot-acl.tasks", PROMPT, position="after_memory")
