"""On-demand profile consultations through the public Hermes CLI; no daemon."""
import json
import logging
import os
import shutil
import signal
import subprocess
import sqlite3
import sys
import tempfile
import contextvars
import hashlib
import re
import time
import uuid
import fcntl
from contextlib import closing, contextmanager


@contextmanager
def _receiver_lock(root, target, mode, request_id):
    """Cross-caller nonblocking admission; native CLI session ownership still applies.

    Never unlink lock files: unlinking permits two different locked inodes.
    Failure to open/lock is an error, never permission to launch a child.
    """
    directory = root / 'state' / 'bot-acl' / 'consult-locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = json.dumps([target, 'Bot Chat' if mode == 'bot_chat' else request_id])
    path = directory / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(fd)
from pathlib import Path

_AUDIT = contextvars.ContextVar('bot_acl_consult_audit', default=None)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def _valid_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _audit(**updates):
    state = _AUDIT.get()
    if state is None:
        return
    state['record'].update(updates)
    path = state['path']
    # Atomic replacement; no question, reply, environment or stderr is recorded.
    fd, temporary = tempfile.mkstemp(prefix='.audit-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state['record'], stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

from hermes_constants import get_hermes_home
from .policy import check

TOOLSET = "bot_consult"
DEPTH_ENV = "BOT_ACL_CONSULT_CHILD"
MAX_TURNS = 250  # Agent iterations, NOT individual tool invocations.
PROMPT = (
    "You can consult another local bot with ask_bot from ordinary sessions. "
    "Use list_bot_peers to discover allowed teammates. Choose mode=new_session "
    "for an isolated consultation, or mode=bot_chat to continue the recipient's "
    "existing Bot Chat. Use mode=resume with target and session_id to continue a "
    "specific returned consultation session. Default to new_session when persistent Bot Chat context "
    "is unnecessary. If bot_chat returns target_busy, follow its next_step; "
    "do not repeatedly retry the busy chat. Replies are tool results, not later "
    "notifications. Calls are bounded one-shot runs: never start a resident agent, "
    "watcher, background service, or polling loop for a consultation."
)
ASK_SCHEMA = {
    "name": "ask_bot",
    "description": "Consult a same-group local bot and receive its reply in this tool result. Runs a one-shot Hermes CLI (default no wall-clock limit), not a resident agent. Budget: 250 agent iterations. Choose new_session, existing bot_chat, or resume with an exact session_id. No automatic retry or mode switch.",
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "target": {"type": "string", "description": "Canonical local profile ID from list_bot_peers."},
            "message": {"type": "string", "description": "Your composed question and necessary context. The recipient does not automatically receive this conversation."},
            "mode": {"type": "string", "enum": ["new_session", "bot_chat", "resume"], "default": "new_session"},
            "session_id": {"type": "string", "description": "Required only for mode=resume: exact session ID in the target profile, not a title or latest. Compression continuations follow the CLI's normal resume rules."},
            "timeout_seconds": {"type": "integer", "oneOf": [{"const": 0}, {"minimum": 30, "maximum": 300}], "default": 0, "description": "0 disables wall-clock limits; finite values 30-300 seconds."},
        },
        "required": ["target", "message"],
    },
}
PEERS_SCHEMA = {
    "name": "list_bot_peers",
    "description": "List canonical local bot IDs allowed by this sender's current bot-acl group. Does not start agents or send messages.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def available():
    return os.environ.get(DEPTH_ENV) != "1" and shutil.which("hermes") is not None


def identity():
    home = Path(get_hermes_home())
    return (home.name, home.parent.parent) if home.parent.name == "profiles" else ("default", home)


def list_peers(domains, args, **kwargs):
    try:
        source, _ = identity()
        groups = [members for members in domains.values() if source in members]
        candidates = groups[0] if len(groups) == 1 else ()
        peers = [name for name in sorted(candidates) if name != source
                 and check(domains, "message_agent", {"target": name}) is None]
        return json.dumps({"source": source, "peers": peers,
                           "modes": ["new_session", "bot_chat", "resume"]}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def _stop_group(proc):
    # Signal only while our leader is still alive; never signal a group ID after
    # communicate()/poll() has reaped it and the kernel may have reused that ID.
    if proc.poll() is None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
    proc.wait(timeout=10)


def _tail(file, limit=524288):
    file.flush()
    file.seek(0, os.SEEK_END)
    file.seek(max(0, file.tell() - limit))
    return file.read().decode("utf-8", errors="replace")


def _run(argv, payload, env, timeout, scratch):
    with tempfile.TemporaryFile(dir=scratch) as output, tempfile.TemporaryFile(dir=scratch) as errors:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                env=env, cwd=str(Path.home()), start_new_session=True)
        timed_out = False
        deadline = time.monotonic() + timeout if timeout else None
        offset, pending, init = 0, b'', None
        dropping = False

        def collect():
            nonlocal offset, pending, init, dropping
            # pread does not move the child's shared stdout file offset.
            while True:
                chunk = os.pread(output.fileno(), 65536, offset)
                if not chunk:
                    break
                offset += len(chunk)
                for part in chunk.splitlines(keepends=True):
                    ended = part.endswith(b'\n')
                    if not dropping:
                        pending += part
                        if len(pending) > 65536:
                            pending, dropping = b'', True
                        elif ended:
                            try:
                                record = json.loads(pending)
                            except (ValueError, TypeError):
                                record = None
                            if isinstance(record, dict) and record.get('type') == 'system' and record.get('subtype') == 'init':
                                sid = _valid_id(record.get('session_id'))
                                if sid:
                                    init = {'type': 'system', 'subtype': 'init', 'session_id': sid}
                                    _audit(session_id=sid)
                    if ended:
                        pending, dropping = b'', False

        try:
            _audit(pid=proc.pid, cleanup='pending', status='running')
            first = True
            while True:
                try:
                    remaining = None if deadline is None else max(.001, deadline - time.monotonic())
                    proc.communicate(input=payload.encode('utf-8') if first else None,
                                     timeout=.1 if remaining is None else min(.1, remaining))
                    collect()
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    collect()
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        _stop_group(proc)
                        collect()
                        break
        finally:
            # Reap on every exit path, but cleanup failures must not replace a
            # KeyboardInterrupt or another exception already in flight.
            if sys.exc_info()[0] is None:
                try:
                    _stop_group(proc)
                    collect()
                    _audit(cleanup='reaped')
                except BaseException:
                    _audit(cleanup='failed')
                    raise
            else:
                try:
                    _stop_group(proc)
                    collect()
                    _audit(cleanup='reaped')
                except Exception:
                    try:
                        _audit(cleanup='failed')
                    except Exception:
                        pass
                    logging.getLogger(__name__).error("bot-acl: consultation cleanup failed during interruption")
        prefix = json.dumps(init) + '\n' if init else ''
        return proc.returncode, prefix + _tail(output), _tail(errors, 8192), timed_out


def _outcome(code, output, errors, timed_out, target, mode):
    base = {"target": target, "mode": mode, "resident": False}
    records = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            records.append(record)
    result = records[-1] if records else None
    # A nonzero CLI exit may still carry a persisted summary. Keep it as
    # explicitly partial evidence, never as a successful/completed reply.
    evidence = {}
    session_id = _valid_id((result or {}).get("session_id"))
    if not session_id:
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict) and record.get('type') == 'system' and record.get('subtype') == 'init':
                session_id = _valid_id(record.get('session_id')) or session_id
    if not session_id:
        session_id = next((_valid_id(line.removeprefix("session_id: ")) for line in reversed(errors.splitlines())
                           if line.startswith("session_id: ") and _valid_id(line.removeprefix("session_id: "))), None)
    if session_id:
        evidence["session_id"] = session_id
    text = (result or {}).get("text")
    if isinstance(text, str) and text.strip():
        evidence.update(partial_reply=text[:32000], truncated=len(text) > 32000)
    diagnostics = "\n".join(line for line in errors.splitlines()
                            if not line.startswith("session_id: ")).strip()
    if timed_out:
        return {**base, "status": "timeout_unknown", "error": "Consultation exceeded its time limit; its process group was stopped. The recipient may already have persisted the question or performed work.",
                "next_step": "Do not automatically resend. Report the timeout and check the recipient session before retrying.",
                **evidence}
    # Prefer a typed refusal, not text from model output (which may quote errors).
    if code != 0 and "hermes-refusal-reason: SESSION_NOT_OWNED" in errors.splitlines():
        return {**base, "status": "target_busy", "error": "The recipient session is owned by another live process; this CLI turn was refused.",
                "next_step": ("Report this session as busy. Do not automatically retry or switch sessions."
                              if mode == "resume" else "If the same question does not require Bot Chat history, call ask_bot again with the same target/message and mode=new_session. Otherwise report that Bot Chat is busy. Do not loop or start a watcher.")}
    if code != 0 or not result or result.get("exit_code") != 0 or result.get("error") or result.get("partial") or result.get("completed") is False or result.get("status") in ("cancelled", "timeout", "partial", "interrupted"):
        return {**base, "status": "failed", "exit_code": code,
                **evidence,
                "error": str((result or {}).get("error") or diagnostics[-2000:] or
                             "CLI did not report successful completion; any partial_reply is incomplete diagnostic output, not proof of task completion."),
                "next_step": "Report the failure and any partial_reply as incomplete; check session_id before retrying. Do not automatically retry or claim the message was not processed."}
    reply = result.get("text", "")
    if not isinstance(reply, str) or not reply.strip():
        return {**base, "status": "failed", **evidence,
                "error": "CLI terminal result has no nonempty string reply"}
    return {**base, "status": "completed", "session_id": session_id,
            "reply": reply[:32000], "truncated": len(reply) > 32000}


def _ask(domains, args, **kwargs):
    try:
        if os.environ.get(DEPTH_ENV) == "1":
            return json.dumps({"status": "blocked", "error": "Nested ask_bot consultations are disabled for one-shot recipients."})
        mode = args.get("mode", "new_session")
        message = args.get("message")
        timeout = args.get("timeout_seconds", 0)
        if mode not in ("new_session", "bot_chat", "resume"):
            raise ValueError("mode must be new_session, bot_chat or resume")
        session_id = args.get("session_id")
        if mode == "resume":
            if not _valid_id(session_id) or session_id.lower() == "latest":
                raise ValueError("resume requires an exact session_id, not a title or latest")
        elif "session_id" in args:
            raise ValueError("session_id is only allowed with mode=resume")
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError("message must contain 1-16000 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not (timeout == 0 or 30 <= timeout <= 300):
            raise ValueError("timeout_seconds must be 0 (unlimited) or an integer between 30 and 300")
        denied = check(domains, "message_agent", args)
        if denied:
            return json.dumps({"status": "blocked", "error": denied["message"]}, ensure_ascii=False)
        raw = args["target"].strip().removeprefix("@")
        target = "default" if raw == "hermes" else raw
        _audit(target=target)
        source, root = identity()
        executable = shutil.which("hermes")
        if not executable:
            raise RuntimeError("Hermes CLI is not available on this host")
        target_home = root if target == "default" else root / "profiles" / target
        if mode == "resume":
            # CLI accepts titles too. Check exact IDs read-only in the selected
            # profile; leave compression and live-writer ownership to the CLI.
            with closing(sqlite3.connect((target_home / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
                if not db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                    raise ValueError("session_id not found in target profile")
            _audit(session_id=session_id)
        env = dict(os.environ)
        env["HERMES_HOME"] = str(target_home)
        env[DEPTH_ENV] = "1"
        scratch = Path(get_hermes_home()) / "cache" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(scratch)
        argv = [executable, "-p", target, "chat", "--in", str(Path.home()),
                "--query-file", "-", "--format", "stream-json", "--source", "ask_bot",
                "--run-budget", str(timeout), "--max-turns", str(MAX_TURNS)]
        if mode == "bot_chat":
            # Resume only; do not create a look-alike Bot Chat if none exists.
            argv += ["--continue", "Bot Chat"]
        elif mode == "resume":
            argv += ["--resume=" + session_id]
        payload = (f"Consultation from @{source}. Reply to this request in your final answer. "
                   "This is a bounded one-shot consultation, not a resident worker. "
                   "Do not launch background agents, daemons, watchers, or polling loops. "
                   "Do not message the sender separately; your final answer is returned to them.\n\n"
                   + message)
        state = _AUDIT.get()
        request_id = state['record']['request_id'] if state else uuid.uuid4().hex
        with _receiver_lock(root, target, mode, session_id if mode == "resume" else request_id) as acquired:
            if not acquired:
                return json.dumps({"target": target, "mode": mode, "resident": False,
                    **({"session_id": session_id} if mode == "resume" else {}),
                    "status": "target_busy", "error": "Another ask_bot process owns this recipient session admission lock.",
                    "next_step": "Report this session as busy. Use mode=new_session only if its history is unnecessary. Do not automatically retry."})
            code, output, errors, timed_out = _run(argv, payload, env, timeout, scratch)
        outcome = _outcome(code, output, errors, timed_out, target, mode)
        if mode == "resume" and not outcome.get("session_id"):
            outcome["session_id"] = session_id
        return json.dumps(outcome, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "resident": False, "error": str(exc)}, ensure_ascii=False)


def ask(domains, args, **kwargs):
    request_id = uuid.uuid4().hex
    directory = Path(get_hermes_home()) / 'state' / 'bot-acl' / 'consult-audit'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {'path': directory / (request_id + '.json'), 'record': {
        'request_id': request_id,
        'implementation_hash': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'pid': None, 'start': time.time(), 'end': None,
        'target': args.get('target') if _valid_id(args.get('target')) else 'invalid',
        'mode': args.get('mode', 'new_session') if args.get('mode', 'new_session') in ('new_session', 'bot_chat', 'resume') else 'invalid',
        'session_id': 'unknown', 'status': 'starting', 'cleanup': 'not_started'}}
    token = _AUDIT.set(state)
    try:
        _audit()
        response = json.loads(_ask(domains, args, **kwargs))
        _audit(status=response['status'], session_id=response.get('session_id') or state['record']['session_id'])
        response['request_id'] = request_id
        return json.dumps(response, ensure_ascii=False)
    except BaseException:
        # Cancellation remains an interruption, never a completed tool reply.
        try:
            _audit(status='cancelled_unknown')
        except Exception:
            logging.getLogger(__name__).error('bot-acl: audit update failed')
        raise
    finally:
        interrupted = sys.exc_info()[0] is not None
        try:
            _audit(end=time.time())
        except Exception:
            if not interrupted:
                raise
            logging.getLogger(__name__).error('bot-acl: final audit write failed')
        finally:
            _AUDIT.reset(token)


def register_consult(ctx, domains):
    ctx.register_tool("ask_bot", TOOLSET, ASK_SCHEMA,
                      lambda args, **kw: ask(domains, args, **kw), check_fn=available,
                      description="Consult a local bot", emoji="💬")
    ctx.register_tool("list_bot_peers", TOOLSET, PEERS_SCHEMA,
                      lambda args, **kw: list_peers(domains, args, **kw), check_fn=available,
                      description="List same-group bots", emoji="🔗")
    ctx.register_system_prompt_section("bot-acl.consult", PROMPT, position="after_memory")
