"""Deterministic public-CLI stand-in used by bot_task tests; no model calls."""
import json
import os
import threading
from pathlib import Path

FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time

def log(event):
    path = os.environ.get("FAKE_CLI_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

argv = sys.argv[1:]
mode = os.environ.get("FAKE_CLI_MODE", "complete")
sid = os.environ.get("FAKE_CLI_SESSION_ID", "20260101_000000_abcdef")
resume = next((arg.split("=", 1)[1] for arg in argv if arg.startswith("--resume=")), None)
target = argv[argv.index("-p") + 1] if "-p" in argv else None
prompt = sys.stdin.read()
log({"event": "start", "pid": os.getpid(), "argv": argv, "target": target,
     "resume": resume, "prompt": prompt, "home": os.environ.get("HERMES_HOME")})
if resume:
    sid = os.environ.get("FAKE_CLI_RESUME_RETURN_ID", resume)
session_evidence = ({} if os.environ.get("FAKE_CLI_NO_SESSION_ID") == "1"
                    else {"session_id": sid})
if session_evidence:
    print(json.dumps({"type": "system", "subtype": "init", **session_evidence}), flush=True)
if os.environ.get("FAKE_CLI_OWNERSHIP_REFUSAL") == "1":
    print("hermes-refusal-reason: SESSION_NOT_OWNED", file=sys.stderr, flush=True)
    log({"event": "refused", "pid": os.getpid()})
    sys.exit(1)
if mode == "partial":
    print(json.dumps({"type": "result", "exit_code": 1, "text": "partial evidence",
                      "error": "quota", **session_evidence}), flush=True)
    log({"event": "end", "pid": os.getpid(), "status": "partial"})
    sys.exit(1)
if mode == "complete":
    print(json.dumps({"type": "result", "exit_code": 0,
                      "text": "reply:" + prompt[-120:], **session_evidence}), flush=True)
    log({"event": "end", "pid": os.getpid(), "status": "complete"})
    sys.exit(0)

def on_term(signum, frame):
    log({"event": "term", "pid": os.getpid()})
    if mode in ("graceful", "tool"):
        print(json.dumps({"type": "result", "exit_code": 130,
                          "status": "interrupted", "text": "interrupted partial",
                          **session_evidence}), flush=True)
        log({"event": "end", "pid": os.getpid(), "status": "interrupted"})
        sys.exit(130)
    if mode == "slowterm":
        time.sleep(float(os.environ.get("FAKE_CLI_TERM_DELAY", "0.5")))
        print(json.dumps({"type": "result", "exit_code": 130,
                          "status": "interrupted", "text": "interrupted partial",
                          **session_evidence}), flush=True)
        log({"event": "end", "pid": os.getpid(), "status": "interrupted"})
        sys.exit(130)
    # ignore mode: keep running so the caller must escalate to SIGKILL.

if mode != "hang":
    signal.signal(signal.SIGTERM, on_term)
if mode in ("tool", "toolignore"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    log({"event": "tool", "pid": child.pid, "parent": os.getpid()})
log({"event": "running", "pid": os.getpid(), "session_id": sid})
while True:
    time.sleep(0.05)
'''


def make_fake_cli(directory):
    path = Path(directory) / "fake-hermes"
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(0o755)
    return path


def read_events(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def starts(path):
    return [event for event in read_events(path) if event["event"] == "start"]


class DummyTurn:
    """Minimal turn double for validation/ownership tests."""

    def __init__(self, session_id="runtime", outcome="completed", reply="result",
                 done=False):
        self.session_id = session_id
        self.requested_session_id = None
        self.outcome = outcome
        self.reply = reply
        self.partial_reply = None
        self.error = None
        self.forced = False
        self.interrupted = False
        self.done = threading.Event()
        if done:
            self.done.set()
        self.interrupt_calls = 0

    def interrupt(self, grace=None, kill_grace=None):
        self.interrupt_calls += 1
        self.interrupted = True
        self.done.set()
        return "graceful"
