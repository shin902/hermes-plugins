"""Deterministic executor selection for the coding profile's task tool.

The caller supplies a role and a task, never an owner, model, or executable.
Only the ``implementation`` role runs the pinned OpenCode worker from
``opencode_worker``. Research, review, and parent roles return an actual
``delegate_task`` payload for Hermes to dispatch with the profile's existing
delegation model configuration; this module never writes that configuration.

Role selection is a closed mapping. No keyword or text heuristic is applied to
the task, so a user merely mentioning code cannot launch OpenCode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from . import opencode_worker


SCHEMA_VERSION: Final = "omh_worker_dispatch/v1"
IMPLEMENTATION_ROLE: Final = "implementation"
NON_IMPLEMENTATION_EXECUTOR: Final = "hermes_delegate_task"
IMPLEMENTATION_EXECUTOR: Final = "opencode"
MAX_CONTEXT_CHARS: Final = 100_000

_ROLES: Final[dict[str, str]] = {
    "implementation": IMPLEMENTATION_ROLE,
    "builder": IMPLEMENTATION_ROLE,
    "research": "research",
    "researcher": "research",
    "review": "review",
    "reviewer": "review",
    "parent": "parent",
}


class DispatchError(ValueError):
    """A request that must fail before any worker process is started."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def canonical_role(value: object) -> str:
    role = str(value or "").strip()
    canonical = _ROLES.get(role)
    if canonical is None:
        raise DispatchError(
            "unknown role; use implementation, research, review, or parent",
            "unknown_role",
        )
    return canonical


def select_executor(role: str) -> dict[str, Any]:
    """The one executor this role is allowed to use."""
    canonical = canonical_role(role)
    if canonical == IMPLEMENTATION_ROLE:
        return {
            "role": canonical,
            "executor": IMPLEMENTATION_EXECUTOR,
            "mode": "execute",
            "model": opencode_worker.WORKER_MODEL,
            "owner_input_accepted": False,
        }
    return {
        "role": canonical,
        "executor": NON_IMPLEMENTATION_EXECUTOR,
        "mode": "return_delegate_payload",
        "model": "",
        "delegation_config": "unchanged",
        "owner_input_accepted": False,
    }


def validate_context(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DispatchError("context must be a string", "invalid_context")
    if "\x00" in value:
        raise DispatchError("context must not contain a NUL byte", "invalid_context")
    if len(value) > MAX_CONTEXT_CHARS:
        raise DispatchError(
            f"context exceeds {MAX_CONTEXT_CHARS} characters", "invalid_context"
        )
    return value


def implementation_prompt(task: str, context: str) -> str:
    prompt = task if not context.strip() else f"{task}\n\nContext:\n{context}"
    try:
        return opencode_worker.validate_prompt(prompt)
    except opencode_worker.WorkerError as exc:
        raise DispatchError(str(exc), "invalid_task") from exc


def delegate_payload(
    task: str, *, context: str = "", cwd: Path | None = None
) -> dict[str, Any]:
    """An argument object matching the exposed ``delegate_task`` schema.

    The schema advertises ``{"tasks": [{"goal", "context"?, ...}], "action"?}``;
    the singular top-level goal and the legacy leaf/orchestrator role are not
    part of it. Hermes derives child capability from depth, so this payload
    contains no role field.
    """
    details = context.strip()
    if cwd is not None:
        details = (details + "\n\n" if details else "") + f"Working directory: {cwd}"
    item: dict[str, Any] = {"goal": task}
    if details:
        item["context"] = details
    return {"tasks": [item]}


def public_result(report: dict[str, Any]) -> dict[str, Any]:
    """The worker report without stderr, transcripts, or caller-owned secrets."""
    return {
        "status": "completed" if report.get("ok") else "failed",
        "ok": bool(report.get("ok")),
        "executor": IMPLEMENTATION_EXECUTOR,
        "model": str(report.get("model") or opencode_worker.WORKER_MODEL),
        "cwd": str(report.get("cwd") or ""),
        "session_id": str(report.get("session_id") or ""),
        "requested_session_id": str(report.get("requested_session_id") or ""),
        "resumed": bool(report.get("resumed")),
        "exit_code": report.get("exit_code"),
        "opencode_exit_code": report.get("opencode_exit_code"),
        "timed_out": bool(report.get("timed_out")),
        "duration_seconds": report.get("duration_seconds"),
        "text": str(report.get("text") or ""),
        "error": str(report.get("error") or ""),
    }
