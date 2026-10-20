"""Bounded host-side GitHub CLI tool for the coding profile."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

GH = "/usr/bin/gh"
CWD = "/home/shi/.hermes/profiles/coding"
TIMEOUT = 20
MAX_OUTPUT = 1_000_000
MAX_TEXT = 20_000
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
ACTIONS = {
    "repo_view", "pr_list", "pr_view", "pr_diff", "pr_checks", "issue_list", "issue_view",
    "run_list", "run_view", "pr_create", "issue_create",
}
COMMON = {"limit"}
BY_ACTION = {
    "repo_view": set(), "pr_list": COMMON | {"state", "head", "base"},
    "pr_view": {"number"}, "pr_diff": {"number"}, "pr_checks": {"number"},
    "issue_list": COMMON | {"state", "assignee", "label"}, "issue_view": {"number"},
    "run_list": COMMON | {"status", "branch", "event"}, "run_view": {"run_id"},
    "pr_create": {"title", "body", "head", "base", "draft", "confirm"},
    "issue_create": {"title", "body", "labels", "assignees", "confirm"},
}
WRITE = {"pr_create", "issue_create"}
RUN_STATUSES = {
    "queued", "completed", "in_progress", "requested", "waiting", "pending",
    "action_required", "cancelled", "failure", "neutral", "skipped", "stale",
    "startup_failure", "success", "timed_out",
}
EVENTS = {
    "branch_protection_rule", "check_run", "check_suite", "create", "delete", "deployment",
    "deployment_status", "discussion", "fork", "gollum", "issue_comment", "issues", "label",
    "merge_group", "milestone", "page_build", "public", "pull_request", "pull_request_review",
    "pull_request_review_comment", "push", "release", "repository_dispatch", "schedule", "status",
    "workflow_dispatch", "workflow_run",
}
STATES = {"open", "closed", "merged", "all"}
REQUIRED_BY_ACTION = {
    "repo_view": [], "pr_list": [], "pr_view": ["number"], "pr_diff": ["number"],
    "pr_checks": ["number"], "issue_list": [], "issue_view": ["number"],
    "run_list": [], "run_view": ["run_id"], "pr_create": ["title", "head", "base", "confirm"],
    "issue_create": ["title", "body", "confirm"],
}

SCHEMA = {
    "name": "host_github",
    "description": "Read GitHub repositories, pull requests, issues, and Actions runs. Create PRs/issues only with confirm=true. Repository is always explicit owner/name.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "repo": {"type": "string", "description": "Explicit owner/name repository."},
            "params": {"type": "object", "description": "Action parameters; see action mapping below.", "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "number": {"type": "integer", "minimum": 1}, "run_id": {"type": "integer", "minimum": 1},
                "state": {"type": "string", "enum": sorted(STATES)}, "head": {"type": "string"},
                "base": {"type": "string"}, "branch": {"type": "string"}, "assignee": {"type": "string"},
                "label": {"type": "string"}, "status": {"type": "string", "enum": sorted(RUN_STATUSES)},
                "event": {"type": "string", "enum": sorted(EVENTS)}, "title": {"type": "string"},
                "body": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}},
                "assignees": {"type": "array", "items": {"type": "string"}}, "draft": {"type": "boolean"},
                "confirm": {"type": "boolean"},
            }, "additionalProperties": False,
                "x-action-parameters": {k: {"allowed": sorted(v), "required": REQUIRED_BY_ACTION[k]}
                                        for k, v in BY_ACTION.items()}},
        }, "required": ["action", "repo", "params"], "additionalProperties": False,
    },
}


def _text(v: Any, name: str, *, max_len: int = MAX_TEXT, empty: bool = False) -> str:
    if not isinstance(v, str) or (not empty and not v.strip()) or len(v) > max_len or "\x00" in v:
        raise ValueError(f"{name} must be a {'string' if empty else 'non-empty string'} of at most {max_len} characters")
    return v


def validate(action: Any, repo: Any, params: Any) -> dict[str, Any]:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unsupported action")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo) or ".." in repo or repo.startswith("-"):
        raise ValueError("repo must be an explicit owner/name")
    allowlist = {item.strip() for item in os.environ.get("HOST_GITHUB_ALLOWED_REPOS", "").split(",") if item.strip()}
    if allowlist and repo not in allowlist:
        raise ValueError("repo is not in HOST_GITHUB_ALLOWED_REPOS")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    unknown = set(params) - BY_ACTION[action]
    if unknown:
        raise ValueError("unknown params: " + ", ".join(sorted(map(str, unknown))))
    p = dict(params)
    for key in ("number", "run_id"):
        if key in p and (not isinstance(p[key], (int, str)) or isinstance(p[key], bool) or not ID_RE.fullmatch(str(p[key]))):
            raise ValueError(f"{key} must be a positive numeric ID")
    if action in {"pr_view", "pr_diff", "pr_checks", "issue_view"} and "number" not in p:
        raise ValueError("number is required")
    if action == "run_view" and "run_id" not in p:
        raise ValueError("run_id is required")
    if "limit" in p and (type(p["limit"]) is not int or not 1 <= p["limit"] <= 100):
        raise ValueError("limit must be 1-100")
    for key, allowed in (("state", STATES), ("status", RUN_STATUSES), ("event", EVENTS)):
        if key in p and (not isinstance(p[key], str) or p[key] not in allowed):
            raise ValueError(f"invalid {key}")
    if action == "issue_list" and p.get("state") == "merged":
        raise ValueError("invalid state for issue_list")
    for key in ("head", "base", "branch"):
        if key in p and _text(p[key], key, max_len=255).startswith("-"):
            raise ValueError(f"{key} cannot start with '-'")
    if "title" in p:
        _text(p["title"], "title", max_len=256)
    if "body" in p:
        _text(p["body"], "body", max_len=MAX_TEXT, empty=True)
    for key in ("assignee", "label"):
        if key in p:
            _text(p[key], key, max_len=100)
    for key in ("labels", "assignees"):
        if key in p:
            if not isinstance(p[key], list) or len(p[key]) > 20:
                raise ValueError(f"{key} must be a list of at most 20 strings")
            for item in p[key]:
                _text(item, key, max_len=100)
    if action in WRITE and "title" not in p:
        raise ValueError("title is required")
    if action == "pr_create":
        for key in ("head", "base"):
            if key not in p:
                raise ValueError(f"{key} is required")
        if "draft" in p and type(p["draft"]) is not bool:
            raise ValueError("draft must be boolean")
    if action == "issue_create" and "body" not in p:
        raise ValueError("body is required")
    if action in WRITE and p.get("confirm") is not True:
        raise ValueError("write requires confirm=true")
    if "confirm" in p and type(p["confirm"]) is not bool:
        raise ValueError("confirm must be boolean")
    return p


def _argv(action: str, repo: str, p: dict[str, Any]) -> list[str]:
    def add(k, flag=None):
        return ([flag or "--" + k.replace("_", "-"), str(p[k])] if k in p else [])
    if action == "repo_view": return [GH, "repo", "view", repo, "--json", "nameWithOwner,description,url,visibility,isArchived,defaultBranchRef"]
    if action == "pr_list": return [GH, "pr", "list", "--repo", repo, "--json", "number,title,state,author,headRefName,baseRefName,url", "--limit", str(p.get("limit", 30))] + sum((add(k) for k in ("state", "head", "base")), [])
    if action in {"pr_view", "pr_diff", "pr_checks"}:
        num = str(p["number"])
        if action == "pr_diff": return [GH, "pr", "diff", num, "--repo", repo]
        if action == "pr_checks": return [GH, "pr", "checks", num, "--repo", repo]
        return [GH, "pr", "view", num, "--repo", repo, "--json", "number,title,state,body,author,headRefName,baseRefName,url,mergedAt"]
    if action == "issue_list": return [GH, "issue", "list", "--repo", repo, "--json", "number,title,state,author,labels,assignees,url", "--limit", str(p.get("limit", 30))] + sum((add(k) for k in ("state", "assignee", "label")), [])
    if action == "issue_view": return [GH, "issue", "view", str(p["number"]), "--repo", repo, "--json", "number,title,state,body,author,labels,assignees,url"]
    if action == "run_list": return [GH, "run", "list", "--repo", repo, "--json", "databaseId,displayTitle,status,conclusion,event,headBranch,createdAt,url", "--limit", str(p.get("limit", 30))] + sum((add(k) for k in ("status", "branch", "event")), [])
    if action == "run_view": return [GH, "run", "view", str(p["run_id"]), "--repo", repo, "--json", "databaseId,displayTitle,status,conclusion,event,headBranch,createdAt,url,jobs"]
    if action == "pr_create":
        a = [GH, "pr", "create", "--repo", repo, "--title", p["title"], "--head", p["head"], "--base", p["base"], "--body", p.get("body", "")]
        if p.get("draft"): a.append("--draft")
        return a
    if action == "issue_create":
        a = [GH, "issue", "create", "--repo", repo, "--title", p["title"], "--body", p["body"]]
        for x in p.get("labels", []): a += ["--label", x]
        for x in p.get("assignees", []): a += ["--assignee", x]
        return a
    raise ValueError("unsupported action")


def _audit(action: str, repo: str, ok: bool) -> None:
    try:
        d = Path(__file__).resolve().parent / "audit"
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        f = d / "calls.jsonl"
        with f.open("a", encoding="utf-8") as out:
            os.chmod(f, 0o600)
            out.write(json.dumps({"time": int(time.time()), "action": action, "repo": repo, "ok": ok}) + "\n")
    except OSError:
        pass


def _redact(s: str) -> str:
    s = re.sub(r"(?i)(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|\bBearer\s+\S+)", "[REDACTED]", s)
    return s


def _error(s: str) -> str:
    return _redact(s)[:2000]


def _run(argv: list[str]) -> tuple[int, str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/shi", "GH_CONFIG_DIR": "/home/shi/.config/gh", "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8"}
    proc = subprocess.Popen(argv, cwd=CWD, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name, pipe):
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            remaining = MAX_OUTPUT - len(buffers[name])
            if remaining > 0:
                buffers[name].extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()

    readers = [threading.Thread(target=drain, args=(name, pipe), daemon=True) for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for reader in readers: reader.start()
    timed_out = False
    try:
        proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    for reader in readers: reader.join()
    for pipe in (proc.stdout, proc.stderr): pipe.close()
    if timed_out: raise RuntimeError("gh timed out")
    if overflow.is_set(): raise RuntimeError("gh output exceeded limit")
    return proc.returncode, bytes(buffers["stdout"]).decode("utf-8", "replace"), bytes(buffers["stderr"]).decode("utf-8", "replace")


def _write_failure(message: str, write_succeeded: bool | None = None) -> str:
    return json.dumps({"ok": False, "error": _error(message), "write_succeeded": write_succeeded,
                       "verified": False, "retry_safe": False}, ensure_ascii=False)


def handle(args: dict[str, Any], **kwargs: Any) -> str:
    action = repo = "unknown"
    write = False
    attempted = False
    try:
        if not isinstance(args, dict) or set(args) != {"action", "repo", "params"}:
            raise ValueError("expected exactly action, repo, params")
        p = validate(args["action"], args["repo"], args["params"])
        action, repo = args["action"], args["repo"]
        write = action in WRITE
        argv = _argv(action, repo, p)
        if write: attempted = True
        rc, out, err = _run(argv)
        if rc:
            msg = "gh failed: " + _redact(err.strip() or f"exit {rc}")
            if write: return _write_failure(msg, None)
            raise RuntimeError(msg)
        result = _redact(out.strip())
        if write:
            match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/(?:pull|issues)/(\d+)\s*", result)
            if not match:
                _audit(action, repo, False)
                return _write_failure("write command completed but target could not be identified; do not retry blindly", True)
            ident = match.group(1)
            pr = action == "pr_create"
            va = _argv("pr_view" if pr else "issue_view", repo, {"number": ident})
            try:
                vrc, vout, verr = _run(va)
                if vrc:
                    _audit(action, repo, False)
                    return _write_failure("write succeeded; readback failed: " + (verr.strip() or f"exit {vrc}"), True)
                readback = json.loads(vout)
                expected = {"title": p["title"], "body": p.get("body", "")}
                if pr: expected.update({"headRefName": p["head"], "baseRefName": p["base"]})
                if not isinstance(readback, dict) or any(readback.get(k) != v for k, v in expected.items()):
                    _audit(action, repo, False)
                    return _write_failure("write succeeded; readback did not match the requested target", True)
            except Exception as exc:
                _audit(action, repo, False)
                return _write_failure("write succeeded; readback failed: " + _redact(str(exc)), True)
            result = json.dumps({"write_succeeded": True, "verified": True, "retry_safe": False,
                                 "created_url": f"https://github.com/{repo}/{'pull' if pr else 'issues'}/{ident}",
                                 "readback": readback}, ensure_ascii=False)
        _audit(action, repo, True)
        return json.dumps({"ok": True, "result": _redact(result)}, ensure_ascii=False)
    except Exception as exc:
        if attempted:
            _audit(action if action in ACTIONS else "unknown", repo if isinstance(repo, str) and REPO_RE.fullmatch(repo) else "unknown", False)
            return _write_failure(str(exc), None)
        # Invalid payloads must not flow into audit metadata.
        safe_action = action if isinstance(action, str) and action in ACTIONS else "unknown"
        safe_repo = repo if isinstance(repo, str) and REPO_RE.fullmatch(repo) else "unknown"
        _audit(safe_action, safe_repo, False)
        return json.dumps({"ok": False, "error": _error(str(exc))}, ensure_ascii=False)


def register(ctx: Any) -> None:
    ctx.register_tool(name="host_github", toolset="host_github", schema=SCHEMA, handler=handle,
                      description="Use the host GitHub CLI through a fixed, validated operation set.")
