"""Bounded host-side GitHub CLI tool."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

GH = "/usr/bin/gh"
GIT = "/usr/bin/git"
TIMEOUT = 20
MAX_OUTPUT = 1_000_000
MAX_TEXT = 20_000
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
ID_RE = re.compile(r"^[1-9][0-9]{0,11}$")
ACTIONS = {
    "repo_view", "pr_list", "pr_view", "pr_diff", "pr_checks", "issue_list", "issue_view",
    "run_list", "run_view", "pr_create", "issue_create",
}
GIT_ACTIONS = {"git_status", "git_diff", "git_log", "git_add", "git_commit", "git_fetch", "git_push"}
ACTIONS |= GIT_ACTIONS
COMMON = {"limit"}
BY_ACTION = {
    "repo_view": set(), "pr_list": COMMON | {"state", "head", "base"},
    "pr_view": {"number"}, "pr_diff": {"number"}, "pr_checks": {"number"},
    "issue_list": COMMON | {"state", "assignee", "label"}, "issue_view": {"number"},
    "run_list": COMMON | {"status", "branch", "event"}, "run_view": {"run_id"},
    "pr_create": {"title", "body", "head", "base", "draft", "confirm"},
    "issue_create": {"title", "body", "labels", "assignees", "confirm"},
}
BY_ACTION.update({
    "git_status": {"cwd"}, "git_diff": {"cwd", "staged"},
    "git_log": {"cwd", "limit"}, "git_add": {"cwd", "paths"},
    "git_commit": {"cwd", "message"},
    "git_fetch": {"cwd", "branch"}, "git_push": {"cwd", "branch"},
})
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

REQUIRED_BY_ACTION.update({
    "git_status": ["cwd"], "git_diff": ["cwd"], "git_log": ["cwd"],
    "git_add": ["cwd", "paths"], "git_commit": ["cwd", "message"],
    "git_fetch": ["cwd", "branch"], "git_push": ["cwd", "branch"],
})

SCHEMA = {
    "name": "host_github",
    "description": "Host Git and GitHub using existing host identity/auth. Git actions use an explicit absolute host cwd without an approval prompt; fetch/push target the explicit GitHub owner/name and branch. PR/issue creation requires confirm=true.",
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
                "cwd": {"type": "string", "description": "Absolute HOST checkout/worktree path, not container /workspace."},
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "message": {"type": "string"}, "staged": {"type": "boolean"},
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
    if action in GIT_ACTIONS:
        missing = set(REQUIRED_BY_ACTION[action]) - set(p)
        if missing:
            raise ValueError("missing params: " + ", ".join(sorted(missing)))
        if not Path(_text(p["cwd"], "cwd", max_len=4096)).is_absolute():
            raise ValueError("cwd must be an absolute host path")
        if "staged" in p and type(p["staged"]) is not bool:
            raise ValueError("staged must be boolean")
        if "paths" in p:
            if not isinstance(p["paths"], list) or not 1 <= len(p["paths"]) <= 100:
                raise ValueError("paths must contain 1-100 relative paths")
            for item in p["paths"]:
                path = _text(item, "path", max_len=4096)
                if Path(path).is_absolute() or ".." in Path(path).parts:
                    raise ValueError("paths must be relative to cwd without '..'")
        if "message" in p:
            _text(p["message"], "message")
        if "branch" in p:
            branch = _text(p["branch"], "branch", max_len=255)
            if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch)
                    or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
                    or ".." in branch or "//" in branch or branch.endswith(("/", "."))):
                raise ValueError("invalid branch")
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


def _home() -> Path:
    """Active profile home, resolved per call instead of pinning an install path."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _run(argv: list[str], *, cwd: str | None = None, timeout: float | None = None) -> tuple[int, str, str]:
    home = Path.home()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "GH_CONFIG_DIR": str(home / ".config/gh"), "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8"}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    proc = subprocess.Popen(argv, cwd=cwd or str(_home()), env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + (TIMEOUT if timeout is None else timeout)
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
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
    for reader in readers: reader.join(max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        timed_out = True
    if timed_out:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    for reader in readers: reader.join(1)
    for pipe in (proc.stdout, proc.stderr): pipe.close()
    if timed_out: raise RuntimeError("gh timed out")
    if overflow.is_set(): raise RuntimeError("gh output exceeded limit")
    return proc.returncode, bytes(buffers["stdout"]).decode("utf-8", "replace"), bytes(buffers["stderr"]).decode("utf-8", "replace")


def _git(action: str, repo: str, p: dict[str, Any]) -> dict[str, Any]:
    """Normal host Git: checkout configuration and hooks are trusted, not sandboxed."""
    cwd = str(Path(p["cwd"]).resolve(strict=True))
    prefix = [GIT, "--no-pager", "--literal-pathspecs"]

    def run(*args: str) -> str:
        rc, out, err = _run(prefix + list(args), cwd=cwd, timeout=120)
        if rc:
            raise RuntimeError("git failed: " + (err.strip() or out.strip() or f"exit {rc}"))
        return out.strip()

    if run("rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError("cwd must be a Git checkout/worktree")
    mutation = action in {"git_add", "git_commit", "git_fetch", "git_push"}
    attempted = False
    succeeded = False
    try:
        if action == "git_status":
            return {"output": run("status", "--short", "--branch")}
        if action == "git_diff":
            return {"output": run("diff", "--no-ext-diff", "--no-textconv", *(["--cached"] if p.get("staged") else []), "--")}
        if action == "git_log":
            return {"output": run("log", "--oneline", "-n", str(p.get("limit", 20)))}
        if action == "git_add":
            attempted = True
            output = run("add", "--", *p["paths"])
            succeeded = True
            return {"output": output, "status": run("status", "--short"), "write_succeeded": True, "verified": True, "retry_safe": False}
        if action == "git_commit":
            attempted = True
            output = run("commit", "-m", p["message"])
            succeeded = True
            return {"output": output, "commit": run("rev-parse", "HEAD"), "write_succeeded": True, "verified": True, "retry_safe": False}
        url = f"https://github.com/{repo}.git"
        ref = "refs/heads/" + p["branch"]
        if action == "git_fetch":
            attempted = True
            output = run("fetch", "--no-recurse-submodules", "--no-tags", url, ref)
            succeeded = True
            return {"output": output, "commit": run("rev-parse", "FETCH_HEAD"),
                    "fetched_ref": ref, "write_succeeded": True, "verified": True, "retry_safe": False}
        if action == "git_push":
            commit = run("rev-parse", "--verify", ref + "^{commit}")
            attempted = True
            output = run("push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", url, commit + ":" + ref)
            succeeded = True
            readback = run("ls-remote", "--refs", url, ref)
            verified = readback.splitlines() == [commit + "\t" + ref]
            return {"ok": verified, "output": output, "commit": commit, "remote_ref": ref,
                    "readback": readback, "write_succeeded": True, "verified": verified, "retry_safe": False}
        raise ValueError("unsupported Git action")
    except Exception as exc:
        return {"ok": False, "error": _error(str(exc)), "verified": False,
                **({"write_succeeded": True if succeeded else None, "retry_safe": False} if mutation and attempted else {})}


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
        if action in GIT_ACTIONS:
            result = _git(action, repo, p)
            ok = result.get("ok", True)
            _audit(action, repo, ok)
            return _redact(json.dumps({"ok": ok, "result": result}, ensure_ascii=False))
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
                      description="Use host Git and GitHub Issue/PR operations with existing host identity and authentication.")
