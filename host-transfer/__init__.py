"""Bounded host-side SSH file transfer to an allowlisted remote."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SSH = "/usr/bin/ssh"
SCP = "/usr/bin/scp"
SHA256SUM = "/usr/bin/sha256sum"
NULL_CONFIG = "/dev/null"
IDENTITY = "/home/shi/.ssh/id_ed25519_mac"
KNOWN_HOSTS = "/home/shi/.ssh/known_hosts"
LOCAL_ROOT = Path("/home/shi")
SSH_DIR = Path("/home/shi/.ssh")
CWD = "/home/shi"
TIMEOUT = 180
MAX_OUTPUT = 200_000

TARGETS = {
    "mac": {"host": "macbook-pro", "user": "shin", "remote_dir": "/Users/shin/Downloads"},
}
ACTIONS = {"probe", "send"}
ALLOWED = {"probe": {"target"}, "send": {"target", "local_path", "confirm"}}
REQUIRED = {"probe": [], "send": ["local_path", "confirm"]}

SCHEMA = {
    "name": "host_transfer",
    "description": "Transfer a host file to an allowlisted remote over SSH. send requires confirm=true and verifies the remote SHA-256; probe is read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "params": {"type": "object", "description": "Action parameters; see x-action-parameters.", "properties": {
                "target": {"type": "string", "enum": sorted(TARGETS), "default": "mac",
                           "description": "Named remote destination; currently only the Mac."},
                "local_path": {"type": "string", "description": "Absolute host path under /home/shi to a regular file."},
                "confirm": {"type": "boolean"},
            }, "additionalProperties": False,
                "x-action-parameters": {k: {"allowed": sorted(ALLOWED[k]), "required": REQUIRED[k]} for k in ACTIONS}},
        }, "required": ["action", "params"], "additionalProperties": False,
    },
}


def _local_file(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("local_path must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("local_path must be absolute")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("local_path does not exist")
    if not real.is_relative_to(LOCAL_ROOT):
        raise ValueError("local_path must be under /home/shi")
    if real == SSH_DIR or SSH_DIR in real.parents:
        raise ValueError("local_path must not reference SSH material")
    if not real.is_file():
        raise ValueError("local_path must be a regular file")
    return str(real)


def validate(action: Any, params: Any) -> dict[str, Any]:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unsupported action")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if any(not isinstance(key, str) for key in params):
        raise ValueError("parameter names must be strings")
    unknown = set(params) - ALLOWED[action]
    if unknown:
        raise ValueError("unknown params: " + ", ".join(sorted(map(str, unknown))))
    missing = set(REQUIRED[action]) - set(params)
    if missing:
        raise ValueError("missing params: " + ", ".join(sorted(missing)))
    p = dict(params)
    target = p.get("target", "mac")
    if not isinstance(target, str) or target not in TARGETS:
        raise ValueError("target is not allowlisted")
    p["target"] = target
    if "confirm" in p and type(p["confirm"]) is not bool:
        raise ValueError("confirm must be boolean")
    if action == "send":
        if p.get("confirm") is not True:
            raise ValueError("send requires confirm=true")
        p["local_path"] = _local_file(p["local_path"])
    return p


def _common_options() -> list[str]:
    # /dev/null avoids host/system SSH config (e.g. unreadable ssh_config.d) and ambient options.
    return ["-F", NULL_CONFIG, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-i", IDENTITY, "-o", "IdentitiesOnly=yes"]


def _remote(target: str) -> str:
    spec = TARGETS[target]
    return f"{spec['user']}@{spec['host']}"


def _probe_argv(target: str) -> list[str]:
    return [SSH, *_common_options(), _remote(target), "echo ok"]


def _send_argv(target: str, local_path: str) -> list[str]:
    return [SCP, *_common_options(), local_path, f"{_remote(target)}:{TARGETS[target]['remote_dir']}/"]


def _remote_sha_argv(target: str, remote_path: str) -> list[str]:
    return [SSH, *_common_options(), _remote(target), f"shasum -a 256 {shlex.quote(remote_path)}"]


def _run(argv: list[str], timeout: float = TIMEOUT) -> tuple[int, str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/shi", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb"}
    proc = subprocess.Popen(argv, cwd=CWD, env=env, shell=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, pipe) -> None:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            remaining = MAX_OUTPUT - len(buffers[name])
            if remaining > 0:
                buffers[name].extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()

    readers = [threading.Thread(target=drain, args=(name, pipe), daemon=True)
               for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, 15)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            proc.wait()
    for reader in readers:
        reader.join()
    for pipe in (proc.stdout, proc.stderr):
        pipe.close()
    if timed_out:
        raise RuntimeError("transfer timed out")
    if overflow.is_set():
        raise RuntimeError("transfer output exceeded limit")
    return proc.returncode, bytes(buffers["stdout"]).decode("utf-8", "replace"), bytes(buffers["stderr"]).decode("utf-8", "replace")


def _redact(text: str) -> str:
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    return re.sub(r"(?i)(token|password|secret|api[_-]?key|passphrase)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", text)


def _audit(action: str, target: str, ok: bool) -> None:
    try:
        directory = Path(__file__).resolve().parent / "audit"
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "calls.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "action": action,
                                     "target": target, "ok": bool(ok)}) + "\n")
    except OSError:
        pass


def _sha256(path: str) -> str:
    rc, out, err = _run([SHA256SUM, path], timeout=30)
    if rc:
        raise RuntimeError("local checksum failed: " + (err.strip() or f"exit {rc}"))
    return out.split()[0]


def _probe(target: str) -> dict[str, Any]:
    rc, out, err = _run(_probe_argv(target), timeout=25)
    reachable = rc == 0 and out.strip() == "ok"
    return {"target": target, "host": TARGETS[target]["host"], "reachable": reachable,
            "detail": None if reachable else _redact((err or out).strip() or f"exit {rc}")}


def _send(target: str, local_path: str) -> dict[str, Any]:
    rc, out, err = _run(_send_argv(target, local_path))
    if rc:
        return {"target": target, "local_path": local_path, "transfer_succeeded": False,
                "verified": False, "retry_safe": False, "error": _redact((err or out).strip() or f"exit {rc}")}
    remote_path = f"{TARGETS[target]['remote_dir']}/{Path(local_path).name}"
    local_hash = _sha256(local_path)
    rrc, rout, rerr = _run(_remote_sha_argv(target, remote_path), timeout=30)
    if rrc:
        return {"target": target, "local_path": local_path, "remote_path": remote_path,
                "transfer_succeeded": True, "verified": False, "retry_safe": False,
                "error": "transfer succeeded; remote checksum failed: " + _redact((rerr or rout).strip() or f"exit {rrc}")}
    remote_hash = rout.split()[0] if rout.split() else ""
    return {"target": target, "local_path": local_path, "remote_path": remote_path,
            "transfer_succeeded": True, "sha256": local_hash, "remote_sha256": remote_hash,
            "verified": bool(remote_hash) and remote_hash == local_hash, "retry_safe": False}


def handle(args: dict[str, Any], **kwargs: Any) -> str:
    action = "unknown"
    target = "unknown"
    try:
        if not isinstance(args, dict) or set(args) != {"action", "params"}:
            raise ValueError("expected exactly action and params")
        action = args["action"]
        p = validate(action, args["params"])
        target = p["target"]
        if action == "probe":
            result = _probe(target)
        else:
            result = _send(target, p["local_path"])
        ok = bool(result.get("verified") if action == "send" else result.get("reachable"))
        _audit(action, target, ok)
        return json.dumps({"ok": ok, "result": result}, ensure_ascii=False)
    except Exception as exc:
        safe_action = action if isinstance(action, str) and action in ACTIONS else "unknown"
        safe_target = target if isinstance(target, str) and target in TARGETS else "unknown"
        _audit(safe_action, safe_target, False)
        return json.dumps({"ok": False, "error": _redact(type(exc).__name__ + ": " + str(exc))}, ensure_ascii=False)


def register(ctx: Any) -> None:
    ctx.register_tool(name="host_transfer", toolset="host_transfer", schema=SCHEMA, handler=handle,
                      description="Transfer a host file to an allowlisted remote over SSH with verified checksums.")
