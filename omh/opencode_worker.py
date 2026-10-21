#!/usr/bin/env python3
"""Profile-local OpenCode implementation worker for the coding profile.

This is deliberately a standalone CLI, not a registered OMH tool. It exists
because the shared OMO runtime currently selects Pi on this host, while the
coding profile's implementation worker must be OpenCode pinned to
``opencode/deepseek-v4.1-flash-free``.

The worker passes only the prompt, an explicit working directory, the pinned
model, and an optional OpenCode session id. It runs ``opencode run --standalone
--format json`` with ``shell=False``, a bounded timeout, and no inherited
Hermes transcript. It prints one JSON result object so the caller can read the
OpenCode session id back and resume it with ``--session``.

It does not modify Hermes' parent, research, or review models, and it does not
register a global worker route. It does not review, verify, commit, push, or
claim merge readiness on its own.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Final


SCHEMA_VERSION: Final = "omh_opencode_worker/v1"
WORKER_MODEL: Final = "opencode/deepseek-v4.1-flash-free"
DEFAULT_TIMEOUT_SECONDS: Final = 900
MAX_TIMEOUT_SECONDS: Final = 3600
MAX_PROMPT_CHARS: Final = 100_000
MAX_STDERR_CHARS: Final = 4_000
MAX_TOOL_ERROR_CHARS: Final = 500
_TOOL_FAILURE_STATES: Final = frozenset(
    {
        "error",
        "failed",
        "failure",
        "denied",
        "rejected",
        "cancelled",
        "canceled",
        "permission_denied",
        "timeout",
        "timed_out",
        "aborted",
    }
)
DEFAULT_OPENCODE_BIN: Final = Path.home() / ".opencode" / "bin" / "opencode"
_SESSION_ID_RE: Final = re.compile(r"^ses_[A-Za-z0-9]{1,120}$")
# OpenCode's step-finish reason schema is stop, length, tool-calls,
# content-filter, error, or unknown. Only ``stop`` is terminal success:
# ``tool-calls`` continues the run, ``length``/``content-filter``/``error`` are
# failures, and ``unknown``/missing is not proof that the step completed.
_TERMINAL_FINISH_REASONS: Final = frozenset({"stop"})


class WorkerError(Exception):
    """A caller-facing validation or launch failure."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def build_argv(*, opencode_bin: Path, prompt: str, session_id: str = "", auto: bool = False) -> list[str]:
    """The exact OpenCode argv. The model is a constant, not a route setting."""
    argv = [
        str(opencode_bin),
        "run",
        "--standalone",
        "--model",
        WORKER_MODEL,
        "--format",
        "json",
    ]
    if session_id:
        argv.extend(["--session", session_id])
    if auto:
        argv.append("--auto")
    # ``--`` keeps a prompt beginning with a dash a positional message.
    argv.extend(["--", prompt])
    return argv


def resolve_cwd(value: str) -> Path:
    """Require an explicit, existing absolute directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkerError("--cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkerError(f"--cwd does not exist: {value}") from exc
    if not resolved.is_dir():
        raise WorkerError(f"--cwd is not a directory: {value}")
    return resolved


def resolve_opencode_bin(value: object) -> Path:
    """Require an explicit executable path; never fall back to a PATH lookup."""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise WorkerError("--opencode-bin must be an absolute path")
    if not path.is_file():
        raise WorkerError(f"OpenCode binary not found: {path}")
    if not os.access(path, os.X_OK):
        raise WorkerError(f"OpenCode binary is not executable: {path}")
    return path


def validate_prompt(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerError("--prompt must be a non-empty string")
    if "\x00" in value:
        raise WorkerError("--prompt must not contain a NUL byte")
    if len(value) > MAX_PROMPT_CHARS:
        raise WorkerError(f"--prompt exceeds {MAX_PROMPT_CHARS} characters")
    return value


def validate_session_id(value: str) -> str:
    if value and not _SESSION_ID_RE.fullmatch(value):
        raise WorkerError("--session must be an OpenCode session id such as ses_abc123")
    return value


def validate_timeout(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerError("--timeout must be an integer number of seconds")
    if value < 1 or value > MAX_TIMEOUT_SECONDS:
        raise WorkerError(f"--timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds")
    return value


def _tool_state_failure(part: object) -> str:
    """A tool part whose final state failed, or an empty string.

    ``opencode run --format json`` emits ``{"type": "tool_use", "part": {...}}``
    events. A denied permission is a tool part with ``state.status == "error"``
    and a rejection message, even when the process exits 0.
    """
    if not isinstance(part, dict) or part.get("type") != "tool":
        return ""
    state = part.get("state")
    if not isinstance(state, dict):
        return ""
    status = state.get("status")
    if not isinstance(status, str):
        return ""
    detail = state.get("error")
    detail_text = detail if isinstance(detail, str) else ""
    normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
    denied = (
        "permission denied" in detail_text.lower()
        or "rejected permission" in detail_text.lower()
    )
    if normalized not in _TOOL_FAILURE_STATES and not denied:
        return ""
    tool = part.get("tool")
    name = tool if isinstance(tool, str) and tool else "unknown"
    message = f"tool {name} ended with state {status}"
    if detail_text:
        message += ": " + detail_text[:MAX_TOOL_ERROR_CHARS]
    return message


def parse_events(stdout: str) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    """Read OpenCode's JSON-lines output into events, session id, text, errors.

    The returned text joins this run's ``text`` parts. It is not a
    reconstructed transcript, so a resumed session reports only what this
    invocation produced.
    """
    events: list[dict[str, Any]] = []
    session_ids: list[str] = []
    texts: list[str] = []
    errors: list[str] = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            errors.append(f"line {number} is not JSON")
            continue
        if not isinstance(event, dict):
            errors.append(f"line {number} is not a JSON object")
            continue
        events.append(event)
        event_type = event.get("type")
        event_type = event_type if isinstance(event_type, str) else ""
        if event_type in {"error", "session.error"}:
            # The executor can report a provider or protocol failure as an
            # event and still exit 0. That is a failed turn, never a success.
            errors.append("output contains an error event")
        elif event_type in {"tool_use", "tool"}:
            failure = _tool_state_failure(event.get("part"))
            if failure:
                errors.append(failure)
        session_id = event.get("sessionID")
        if isinstance(session_id, str) and session_id:
            session_ids.append(session_id)
        part = event.get("part")
        if (
            event.get("type") == "text"
            and isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            texts.append(part["text"])
    unique_sessions = sorted(set(session_ids))
    if len(unique_sessions) > 1:
        errors.append("output contains more than one session id")
    return events, (unique_sessions[0] if len(unique_sessions) == 1 else ""), "\n".join(texts), errors


def _finish_reason(event: dict[str, Any]) -> str:
    part = event.get("part")
    if not isinstance(part, dict):
        return ""
    reason = part.get("reason")
    return reason if isinstance(reason, str) else ""


def _completion_observed(events: list[dict[str, Any]]) -> bool:
    """Whether the last OpenCode step ended with a terminal finish reason.

    Text parts are not completion evidence: a step can emit intermediate text
    and still be truncated. A later ``step_start`` clears an earlier terminal
    finish, so a completed step cannot mask a later unfinished one.
    """
    completed = False
    for event in events:
        event_type = event.get("type")
        if event_type == "step_start":
            completed = False
        elif event_type == "step_finish":
            completed = _finish_reason(event) in _TERMINAL_FINISH_REASONS
    return completed


def _terminate(process: subprocess.Popen[str]) -> None:
    """Stop the private OpenCode process group on timeout."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()


def _bounded_stderr(value: str) -> str:
    text = value.strip()
    return text[-MAX_STDERR_CHARS:] if len(text) > MAX_STDERR_CHARS else text


def run_worker(
    *,
    cwd: Path,
    prompt: str,
    session_id: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    opencode_bin: Path = DEFAULT_OPENCODE_BIN,
    auto: bool = False,
) -> dict[str, Any]:
    """Run one bounded OpenCode turn and return an honest JSON-ready report."""
    argv = build_argv(
        opencode_bin=opencode_bin, prompt=prompt, session_id=session_id, auto=auto
    )
    started = time.monotonic()
    # OpenCode resolves its project directory from PWD, not only from the
    # process cwd. Set both so an inherited shell PWD cannot redirect edits
    # into a different repository.
    environment = {**os.environ, "PWD": str(cwd)}
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorkerError(f"could not start OpenCode: {exc}") from exc

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate(process)
        raise

    duration = round(time.monotonic() - started, 3)
    events, output_session, text, parse_errors = parse_events(stdout or "")
    actual_exit = None if timed_out else process.returncode

    error = ""
    final_exit = 0
    if timed_out:
        error = f"OpenCode timed out after {timeout} seconds"
        final_exit = 124
    elif process.returncode != 0:
        # Stderr stays in its own field: it can carry provider detail or
        # secrets, and the tool-facing report omits it.
        error = f"OpenCode exited with code {process.returncode}"
        final_exit = max(1, min(int(process.returncode), 255))
    elif parse_errors:
        error = "OpenCode JSON output was invalid: " + "; ".join(parse_errors[:3])
        final_exit = 1
    elif not output_session:
        error = "OpenCode JSON output did not include a session id"
        final_exit = 1
    elif session_id and output_session != session_id:
        error = (
            f"OpenCode returned session {output_session} instead of requested "
            f"session {session_id}; refusing to report a resumed turn"
        )
        final_exit = 1
    elif not _completion_observed(events):
        # A session id, text, an initial step, or a successful tool event is
        # partial stream evidence, not proof that the turn completed.
        error = "OpenCode JSON output did not include a terminal step finish with reason 'stop'"
        final_exit = 1

    ok = not error
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "model": WORKER_MODEL,
        "cwd": str(cwd),
        "session_id": output_session,
        "requested_session_id": session_id,
        "resumed": bool(session_id and output_session == session_id),
        "auto": auto,
        "exit_code": final_exit,
        "opencode_exit_code": actual_exit,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "text": text,
    }
    if error:
        report["error"] = error
        if stderr:
            report["stderr"] = _bounded_stderr(stderr)
    return report


def _error_report(error: str, exit_code: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "model": WORKER_MODEL,
        "exit_code": exit_code,
        "error": error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the coding profile's external implementation worker with a pinned "
            "OpenCode model and resumable JSON session output."
        )
    )
    parser.add_argument("--cwd", required=True, help="Absolute working directory for OpenCode.")
    parser.add_argument("--prompt", required=True, help="Implementation request to send.")
    parser.add_argument(
        "--session",
        default="",
        help="Existing OpenCode session id to resume; the returned id resumes with this flag.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Bounded run time in seconds (1-{MAX_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--opencode-bin",
        type=Path,
        default=DEFAULT_OPENCODE_BIN,
        help="Absolute OpenCode executable path.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Explicitly pass OpenCode --auto. Omitted by default so permissions are not auto-approved.",
    )
    args = parser.parse_args(argv)

    try:
        report = run_worker(
            cwd=resolve_cwd(args.cwd),
            prompt=validate_prompt(args.prompt),
            session_id=validate_session_id(args.session),
            timeout=validate_timeout(args.timeout),
            opencode_bin=resolve_opencode_bin(args.opencode_bin),
            auto=args.auto,
        )
    except WorkerError as exc:
        print(json.dumps(_error_report(str(exc), exc.exit_code), sort_keys=True))
        return exc.exit_code
    except KeyboardInterrupt:
        print(json.dumps(_error_report("interrupted", 130), sort_keys=True))
        return 130

    print(json.dumps(report, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
