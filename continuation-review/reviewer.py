"""Conservative Jev reviewer for one explicitly selected Hermes session."""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
OPTIONS = ("continue", "stop", "wait")
MIN_CONFIDENCE = 0.40
MIN_CONTINUE_PROBABILITY = 0.60
MIN_CONTINUE_MARGIN = 0.20
QUESTION = {
    "type": "choice",
    "instructions": (
        "Continue only an explicit, user-authorized execution task with a concrete requested "
        "deliverable that is clearly unfinished. Mere questions, explanations, advice, feasibility "
        "discussions, smalltalk, acknowledgments, or answered informational requests are not "
        "execution tasks: stop, even if an older task in the transcript was unfinished but later "
        "cancelled or superseded. Stop when the requested deliverable is complete or cancelled. "
        "Wait for approval, user input, or a subagent; never override a wait. Do not propose or "
        "authorize broader work. If unclear, wait."
    ),
    "criteria": {
        "continue": "An explicit authorized execution task has a concrete deliverable that remains unfinished.",
        "stop": "The user cancelled; the deliverable is complete; or the latest request is merely informational, discussion, advice, smalltalk, or acknowledgment.",
        "wait": "Approval, user input, subagent completion, or more evidence is needed; ambiguity means wait.",
    },
}


class ReviewError(ValueError):
    """Invalid input, unavailable transport, or malformed Jev response."""


def _jev_key():
    """Read only the authorized TypeSafe key, without sourcing any .env file."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    try:
        lines = (home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name.strip() == "TYPESAFE_API_KEY":
            value = value.strip()
            if value and value[0:1] in ("\"", "'"):
                try:
                    value = shlex.split(value, comments=False, posix=True)[0]
                except (ValueError, IndexError):
                    raise ReviewError("TYPESAFE_API_KEY in .env is malformed")
            return value or None
    return None


def export_session(session_id, runner=subprocess.run):
    """Read one exact session via Hermes' public JSONL export CLI."""
    sid = _validate_session_id(session_id)
    try:
        result = runner(["hermes", "sessions", "export", "-", "--format", "jsonl",
                         "--session-id", sid], check=True, capture_output=True,
                        text=True, timeout=60)
        record = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ReviewError("public Hermes session export failed: " + type(exc).__name__)
    if record.get("id") != sid or not isinstance(record.get("messages"), list):
        raise ReviewError("public session export did not return the requested session and messages")
    messages = [{"role": m.get("role"), "content": m.get("content")}
                for m in record["messages"] if m.get("role") in ("user", "assistant", "tool")]
    transcript = json.dumps(messages[-120:], ensure_ascii=False)
    if len(transcript) > 200_000:
        transcript = transcript[-200_000:]
    return {"session_id": sid, "transcript": transcript,
            "cancelled": False, "awaiting_approval": False,
            "subagent_running": False, "session_active": False}


def _validate_session_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value) or value.casefold() == "latest":
        raise ReviewError("session_id must be one exact Hermes session ID, not a CLI alias")
    return value


def _validate_case(case):
    if not isinstance(case, dict):
        raise ReviewError("input must be a JSON object")
    sid = _validate_session_id(case.get("session_id"))
    transcript = case.get("transcript")
    if not isinstance(transcript, (str, list, dict)) or not transcript:
        raise ReviewError("transcript must be non-empty text or structured JSON")
    for key in ("cancelled", "awaiting_approval", "subagent_running", "session_active"):
        value = case.get(key)
        if not isinstance(value, bool):
            raise ReviewError(key + " must be explicitly set to a boolean")
    return sid


def request_payload(case):
    _validate_case(case)
    return {
        "model": MODEL,
        "state": case,
        "questions": {"decision": QUESTION},
    }


def _parse_response(response):
    try:
        answer = response["answers"]["decision"]
        choice = answer["choice"]
        probabilities = answer["probabilities"]
        confidence = answer["confidence"]
    except (KeyError, TypeError):
        raise ReviewError("Jev response is missing the documented choice fields")
    if answer.get("type") != "choice" or choice not in OPTIONS:
        raise ReviewError("Jev returned an unsupported decision")
    if not isinstance(probabilities, dict) or set(probabilities) != set(OPTIONS):
        raise ReviewError("Jev probabilities do not match the decision options")
    values = list(probabilities.values())
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 or x > 1 for x in values):
        raise ReviewError("Jev returned invalid probabilities")
    if abs(sum(values) - 1.0) > 0.02:
        raise ReviewError("Jev probabilities do not sum to one")
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ReviewError("Jev returned invalid confidence")
    return choice, float(confidence), probabilities


def decide(case, transport=None, live=False):
    """Return a typed decision. transport(payload) may be injected for tests."""
    sid = _validate_case(case)
    if case.get("cancelled"):
        decision, confidence, probs, reason = "stop", 1.0, {"continue": 0.0, "stop": 1.0, "wait": 0.0}, "cancelled"
    elif case.get("awaiting_approval") or case.get("subagent_running") or case.get("session_active"):
        decision, confidence, probs, reason = "wait", 1.0, {"continue": 0.0, "stop": 0.0, "wait": 1.0}, "session_or_dependency_not_idle"
    elif not live and transport is None:
        decision, confidence, probs, reason = "wait", 0.0, {}, "dry_run_no_external_review"
    else:
        payload = request_payload(case)
        if transport is None:
            key = _jev_key()
            if not key:
                raise ReviewError("TYPESAFE_API_KEY is missing from the authorized environment and .env files")
            transport = lambda body: _post_jev(body, key)
        choice, confidence, probs = _parse_response(transport(payload))
        continue_margin = probs["continue"] - max(probs["stop"], probs["wait"])
        accepted = confidence >= MIN_CONFIDENCE
        if choice == "continue":
            accepted = (accepted and probs["continue"] >= MIN_CONTINUE_PROBABILITY
                        and continue_margin >= MIN_CONTINUE_MARGIN)
        decision = choice if accepted else "wait"
        reason = "jev_choice" if decision == choice else "low_confidence"
    return {
        "decision": decision,
        "session_id": sid,
        "confidence": confidence,
        "probabilities": probs,
        "reason": reason,
        "resume_command": ["hermes", "--resume", sid] if decision == "continue" else None,
        "resume_executed": False,
    }


def _post_jev(payload, api_key):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > 32 * 1024 * 1024:
        raise ReviewError("Jev request exceeds the documented 32 MiB body limit")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(4 * 1024 * 1024 + 1))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ReviewError("Jev request failed; no session was resumed: " + type(exc).__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review one Hermes session; never resumes or sends a continuation.")
    parser.add_argument("case", type=Path, help="JSON file containing session_id, transcript, and optional safety flags")
    parser.add_argument("--live", action="store_true", help="explicitly send the supplied transcript to TypeSafe Jev")
    args = parser.parse_args(argv)
    try:
        case = json.loads(args.case.read_text(encoding="utf-8"))
        print(json.dumps(decide(case, live=args.live), ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, ReviewError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
