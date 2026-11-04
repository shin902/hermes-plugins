#!/usr/bin/env python3
"""Small operator CLI for Codex usage and the profile-local task queue."""
import argparse
import fcntl
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import actions
import monitor

HOME = actions.HOME
STATE = HOME / "plugin-data/codex-usage/state.json"
QUEUE = actions.QUEUE


def setup_cli(p):
    sub = p.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Show cached usage, or fetch fresh usage")
    status.add_argument("--fresh", action="store_true")
    tasks = sub.add_parser("tasks", help="Manage the durable task queue")
    task_sub = tasks.add_subparsers(dest="action", required=True)
    task_sub.add_parser("list")
    add = task_sub.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--cwd", required=True, type=Path)
    add.add_argument("--prompt", required=True)
    add.add_argument("--model")
    add.add_argument("--session-id", help="Resume this exact Codex session when the queued task starts")
    add.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    remove = task_sub.add_parser("remove")
    remove.add_argument("id")
    resume = sub.add_parser("resume", help="Continue one exact Codex session UUID")
    resume.add_argument("session_id")
    resume.add_argument("--prompt", required=True)
    resume.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    resume.add_argument("--cwd", type=Path, default=Path.cwd)


def parser():
    p = argparse.ArgumentParser(prog="codex-usage")
    setup_cli(p)
    return p


def queue_edit(fn):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with actions.STATE.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        if not isinstance(queue, list):
            raise ValueError("tasks.json must contain a JSON array")
        return fn(queue)


def run(args):
    if args.command == "status":
        if args.fresh:
            monitor.run(state_path=STATE)
            stored = json.loads(STATE.read_text())
            snapshot = stored["snapshot"]
        else:
            stored = json.loads(STATE.read_text()) if STATE.exists() else {}
            snapshot = stored.get("snapshot")
            if snapshot is None:
                raise ValueError("No cached snapshot; run `codex-usage status --fresh`")
        print(json.dumps(snapshot, sort_keys=True, indent=2))
    elif args.command == "tasks" and args.action == "list":
        queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        print(json.dumps(queue, sort_keys=True, indent=2))
    elif args.command == "tasks" and args.action == "add":
        if not args.id.strip() or not args.prompt.strip():
            raise ValueError("task id and prompt must be non-empty")
        if not args.cwd.is_absolute():
            raise ValueError("task cwd must be an absolute path")
        cwd = args.cwd.resolve(strict=True)
        task = {"id": args.id, "cwd": str(cwd), "prompt": args.prompt}
        if getattr(args, "session_id", None):
            task["session_id"] = actions.session_uuid(args.session_id)
        if args.model:
            task["model"] = args.model
        if args.reasoning_effort:
            task["reasoning_effort"] = args.reasoning_effort
        def add(queue):
            if any(not isinstance(x, dict) or not isinstance(x.get("id"), str) for x in queue):
                raise ValueError("tasks.json contains an invalid task")
            if any(x["id"] == args.id for x in queue):
                raise ValueError(f"Task id already queued: {args.id}")
            actions.atomic_json(QUEUE, [*queue, task])
        queue_edit(add)
        print(f"Queued {args.id}")
    elif args.command == "tasks" and args.action == "remove":
        def remove(queue):
            remaining = [x for x in queue if not isinstance(x, dict) or x.get("id") != args.id]
            if len(remaining) == len(queue):
                raise ValueError(f"Task id not queued: {args.id}")
            actions.atomic_json(QUEUE, remaining)
        queue_edit(remove)
        print(f"Removed {args.id}")
    elif args.command == "resume":
        session_id = actions.session_uuid(args.session_id)
        result = actions.invoke(args.prompt, args.cwd, session_id=session_id, sandbox=args.sandbox)
        print(result.stdout, end="")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        run(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        p.exit(1, f"codex-usage: {exc}\n")


if __name__ == "__main__":
    main()
