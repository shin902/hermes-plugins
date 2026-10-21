from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .. import opencode_worker, worker_dispatch

OMH_WORKER_SCHEMA = {
    "name": "omh_worker",
    "description": (
        "Automatic task-delegation entry for this coding profile. Supply a structured "
        "role and task only; never an owner, model, provider, or executable. role="
        "implementation automatically runs the pinned OpenCode implementation worker "
        "(opencode/deepseek-v4.1-flash-free) with an explicit absolute cwd, bounded "
        "timeout, and optional OpenCode session resume. role=research, review, or parent "
        "returns an actual delegate_task payload to dispatch through Hermes with the "
        "existing delegation model configuration; it does not execute OpenCode or change "
        "that configuration. action=select previews the selection without executing. "
        "Unknown roles fail closed. This tool does not intercept delegate_task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["dispatch", "select"],
                "description": "dispatch executes implementation or returns a delegate payload; select only previews.",
                "default": "dispatch",
            },
            "role": {
                "type": "string",
                "enum": [
                    "implementation",
                    "builder",
                    "research",
                    "researcher",
                    "review",
                    "reviewer",
                    "parent",
                ],
                "description": "Structured work role. Aliases builder/researcher/reviewer are exact, not keyword matches.",
            },
            "task": {
                "type": "string",
                "description": "Complete task or prompt for the selected executor.",
            },
            "cwd": {
                "type": "string",
                "description": "Absolute existing working directory. Required for implementation dispatch.",
            },
            "context": {
                "type": "string",
                "description": "Optional context for the task. Implementation appends it to the worker prompt; delegation adds it to the payload.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    f"Bounded OpenCode run time, 1-{opencode_worker.MAX_TIMEOUT_SECONDS} "
                    f"seconds (default {opencode_worker.DEFAULT_TIMEOUT_SECONDS})."
                ),
                "default": opencode_worker.DEFAULT_TIMEOUT_SECONDS,
            },
            "session_id": {
                "type": "string",
                "description": "Existing OpenCode session id to resume. Implementation only; omit to let OpenCode create one.",
            },
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    },
}

WORKER_DISPATCH_SECTION_ID = "omh.worker-dispatch"
WORKER_DISPATCH_SECTION_MAX_CHARS = 2000
WORKER_DISPATCH_GUIDANCE = (
    "[OMH coding worker dispatch]\n"
    "This coding profile has one automatic task-delegation entry: `omh_worker`. "
    "Call it with a structured `role` and `task`; do not choose or pass an owner, "
    "model, provider, or executable.\n"
    "- For work that changes code, use role=\"implementation\" with an explicit "
    "absolute `cwd` and a complete task. OMH automatically selects and runs the "
    "pinned OpenCode implementation worker (opencode/deepseek-v4.1-flash-free) with "
    "a bounded timeout and optional `session_id` resume. Do not launch OpenCode "
    "yourself and do not call `omh_delegate_route` for this role.\n"
    "- For role=\"research\", \"review\", or \"parent\", `omh_worker` returns the exact "
    "`delegate_task` payload to dispatch through Hermes. Hermes keeps its existing "
    "delegation path and model configuration; OMH does not alter it.\n"
    "Unknown roles fail closed. `omh_worker` runs only when called. It does not "
    "intercept `delegate_task` or the existing team flow."
)
_ALLOWED_ARGUMENTS = frozenset(OMH_WORKER_SCHEMA["parameters"]["properties"])


def _refused(reason: str, message: str) -> str:
    return json.dumps(
        {
            "schema_version": worker_dispatch.SCHEMA_VERSION,
            "status": "refused",
            "ok": False,
            "reason": reason,
            "error": message,
            "executed": False,
        },
        sort_keys=True,
    )


def _text_argument(args: dict[str, Any], name: str) -> str:
    value = args.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise worker_dispatch.DispatchError(f"{name} must be a string", f"invalid_{name}")
    return value


def _resolved_cwd(value: str) -> Path:
    try:
        return opencode_worker.resolve_cwd(value)
    except opencode_worker.WorkerError as exc:
        raise worker_dispatch.DispatchError(str(exc), "invalid_cwd") from exc


def omh_worker_handler(
    args: dict[str, Any], *, profile_name: str = "", **kwargs: object
) -> str:
    del kwargs  # Host context is not an execution or identity input.
    if not isinstance(args, dict):
        return _refused("invalid_arguments", "arguments must be an object")
    if str(profile_name or "").strip() != "coding":
        return _refused(
            "profile_not_coding",
            "omh_worker is available only in the coding profile",
        )

    unknown = sorted(set(args) - _ALLOWED_ARGUMENTS)
    if unknown:
        return _refused(
            "unknown_arguments",
            "unknown arguments: " + ", ".join(unknown),
        )

    action = str(args.get("action", "dispatch") or "dispatch").strip().lower()
    if action not in {"dispatch", "select"}:
        return _refused("unknown_action", "action must be dispatch or select")

    try:
        role = worker_dispatch.canonical_role(args.get("role"))
        selection = worker_dispatch.select_executor(role)
        task = _text_argument(args, "task")
        try:
            task = opencode_worker.validate_prompt(task)
        except opencode_worker.WorkerError as exc:
            raise worker_dispatch.DispatchError(str(exc), "invalid_task") from exc
        context = worker_dispatch.validate_context(args.get("context"))
        cwd_text = _text_argument(args, "cwd")
        session_id = _text_argument(args, "session_id")
        try:
            session_id = opencode_worker.validate_session_id(session_id)
        except opencode_worker.WorkerError as exc:
            raise worker_dispatch.DispatchError(str(exc), "invalid_session") from exc
        timeout_value = args.get("timeout_seconds", opencode_worker.DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout = opencode_worker.validate_timeout(timeout_value)
        except opencode_worker.WorkerError as exc:
            raise worker_dispatch.DispatchError(str(exc), "invalid_timeout") from exc

        implementation = role == worker_dispatch.IMPLEMENTATION_ROLE
        if not implementation and session_id:
            raise worker_dispatch.DispatchError(
                "session_id is only valid for the implementation role",
                "invalid_session",
            )
        # Validate the final task+context prompt here so an oversized combined
        # request is refused by this try block, before any process is spawned.
        prompt = (
            worker_dispatch.implementation_prompt(task, context)
            if implementation
            else ""
        )
        if implementation and action == "dispatch" and not cwd_text:
            raise worker_dispatch.DispatchError(
                "cwd is required for implementation dispatch", "invalid_cwd"
            )
        cwd = _resolved_cwd(cwd_text) if cwd_text else None
    except worker_dispatch.DispatchError as exc:
        return _refused(exc.reason, str(exc))

    if action == "select":
        payload: dict[str, Any] = {
            "schema_version": worker_dispatch.SCHEMA_VERSION,
            "action": action,
            "status": "selected",
            "ok": True,
            "role": role,
            "selection": selection,
            "requires_cwd": implementation,
            "executed": False,
        }
        if not implementation:
            payload["delegate_task"] = worker_dispatch.delegate_payload(
                task, context=context, cwd=cwd
            )
        return json.dumps(payload, sort_keys=True)

    if not implementation:
        payload = {
            "schema_version": worker_dispatch.SCHEMA_VERSION,
            "action": action,
            "status": "payload_ready",
            "ok": True,
            "role": role,
            "selection": selection,
            "delegate_task": worker_dispatch.delegate_payload(
                task, context=context, cwd=cwd
            ),
            "executed": False,
            "evidence_boundary": (
                "The delegate_task payload is prepared only. Hermes dispatch, child "
                "execution, verification, review, and completion are not observed here."
            ),
        }
        return json.dumps(payload, sort_keys=True)

    if cwd is None:  # Defensive: checked above, never reached by dispatch.
        return _refused("invalid_cwd", "cwd is required for implementation dispatch")

    try:
        report = opencode_worker.run_worker(
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            timeout=timeout,
            auto=False,
        )
    except opencode_worker.WorkerError as exc:
        return json.dumps(
            {
                "schema_version": worker_dispatch.SCHEMA_VERSION,
                "action": action,
                "status": "failed",
                "ok": False,
                "role": role,
                "selection": selection,
                "executed": True,
                "error": str(exc),
                "exit_code": exc.exit_code,
            },
            sort_keys=True,
        )
    except Exception as exc:  # A tool handler must never raise into the host.
        return json.dumps(
            {
                "schema_version": worker_dispatch.SCHEMA_VERSION,
                "action": action,
                "status": "failed",
                "ok": False,
                "role": role,
                "selection": selection,
                "executed": True,
                "error": f"worker failed: {type(exc).__name__}",
                "exit_code": 1,
            },
            sort_keys=True,
        )

    payload = {
        "schema_version": worker_dispatch.SCHEMA_VERSION,
        "action": action,
        "role": role,
        "selection": selection,
        "executed": True,
    }
    payload.update(worker_dispatch.public_result(report))
    return json.dumps(payload, sort_keys=True)


def worker_dispatch_system_prompt_section(session_info: object) -> str:
    if not isinstance(session_info, Mapping):
        return ""
    if str(session_info.get("profile_name") or "").strip() != "coding":
        return ""
    return WORKER_DISPATCH_GUIDANCE
