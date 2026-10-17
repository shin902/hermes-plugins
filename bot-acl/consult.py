"""On-demand profile consultations through the public Hermes CLI; no daemon."""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from hermes_constants import get_hermes_home
from .policy import check

TOOLSET = "bot_consult"
DEPTH_ENV = "BOT_ACL_CONSULT_CHILD"
MAX_TURNS = 100
PROMPT = (
    "You can consult another local bot with ask_bot from ordinary sessions. "
    "Use list_bot_peers to discover allowed teammates. Choose mode=new_session "
    "for an isolated consultation, or mode=bot_chat to continue the recipient's "
    "existing Bot Chat. Default to new_session when persistent Bot Chat context "
    "is unnecessary. If bot_chat returns target_busy, follow its next_step; "
    "do not repeatedly retry the busy chat. Replies are tool results, not later "
    "notifications. Calls are bounded one-shot runs: never start a resident agent, "
    "watcher, background service, or polling loop for a consultation."
)
ASK_SCHEMA = {
    "name": "ask_bot",
    "description": "Consult a same-group local bot and receive its reply in this tool result. Runs a bounded one-shot Hermes CLI, not a resident agent. Choose an isolated new session or the bot's existing Bot Chat. No automatic retry or mode switch.",
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "target": {"type": "string", "description": "Canonical local profile ID from list_bot_peers."},
            "message": {"type": "string", "description": "Your composed question and necessary context. The recipient does not automatically receive this conversation."},
            "mode": {"type": "string", "enum": ["new_session", "bot_chat"], "default": "new_session"},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 300, "default": 180},
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
                           "modes": ["new_session", "bot_chat"]}, ensure_ascii=False)
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
        try:
            try:
                proc.communicate(input=payload.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_group(proc)
        finally:
            # Reap on every exit path, but cleanup failures must not replace a
            # KeyboardInterrupt or another exception already in flight.
            if sys.exc_info()[0] is None:
                _stop_group(proc)
            else:
                try:
                    _stop_group(proc)
                except Exception:
                    logging.getLogger(__name__).exception("bot-acl: consultation cleanup failed during interruption")
        return proc.returncode, _tail(output), _tail(errors, 8192), timed_out


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
    session_id = (result or {}).get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = next((line.removeprefix("session_id: ") for line in reversed(errors.splitlines())
                           if line.startswith("session_id: ")), None)
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
                "next_step": "If the same question does not require Bot Chat history, call ask_bot again with the same target/message and mode=new_session. Otherwise report that Bot Chat is busy. Do not loop or start a watcher."}
    if code != 0 or not result or result.get("exit_code") != 0 or result.get("error"):
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


def ask(domains, args, **kwargs):
    try:
        if os.environ.get(DEPTH_ENV) == "1":
            return json.dumps({"status": "blocked", "error": "Nested ask_bot consultations are disabled for one-shot recipients."})
        mode = args.get("mode", "new_session")
        message = args.get("message")
        timeout = args.get("timeout_seconds", 180)
        if mode not in ("new_session", "bot_chat"):
            raise ValueError("mode must be new_session or bot_chat")
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError("message must contain 1-16000 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 300:
            raise ValueError("timeout_seconds must be an integer between 30 and 300")
        denied = check(domains, "message_agent", args)
        if denied:
            return json.dumps({"status": "blocked", "error": denied["message"]}, ensure_ascii=False)
        raw = args["target"].strip().removeprefix("@")
        target = "default" if raw == "hermes" else raw
        source, root = identity()
        executable = shutil.which("hermes")
        if not executable:
            raise RuntimeError("Hermes CLI is not available on this host")
        target_home = root if target == "default" else root / "profiles" / target
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
        payload = (f"Consultation from @{source}. Reply to this request in your final answer. "
                   "This is a bounded one-shot consultation, not a resident worker. "
                   "Do not launch background agents, daemons, watchers, or polling loops. "
                   "Do not message the sender separately; your final answer is returned to them.\n\n"
                   + message)
        code, output, errors, timed_out = _run(argv, payload, env, timeout, scratch)
        return json.dumps(_outcome(code, output, errors, timed_out, target, mode), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "resident": False, "error": str(exc)}, ensure_ascii=False)


def register_consult(ctx, domains):
    ctx.register_tool("ask_bot", TOOLSET, ASK_SCHEMA,
                      lambda args, **kw: ask(domains, args, **kw), check_fn=available,
                      description="Consult a local bot", emoji="💬")
    ctx.register_tool("list_bot_peers", TOOLSET, PEERS_SCHEMA,
                      lambda args, **kw: list_peers(domains, args, **kw), check_fn=available,
                      description="List same-group bots", emoji="🔗")
    ctx.register_system_prompt_section("bot-acl.consult", PROMPT, position="after_memory")
