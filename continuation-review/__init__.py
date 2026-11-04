"""Registered post-turn Jev continuation reviewer."""
import json
import logging

from .reviewer import ReviewError, decide

log = logging.getLogger(__name__)
CONTINUE_MESSAGE = (
    "Continue the unfinished, already-authorized task now. Keep the original scope. "
    "If approval, user input, or a child task is still required, wait instead."
)


def _history_text(history):
    """Bound hook evidence and keep only conversational roles/content."""
    messages = []
    for item in history[-120:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant", "tool"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": item["role"], "content": content})
        elif isinstance(content, list):
            messages.append({"role": item["role"], "content": content})
    text = json.dumps(messages, ensure_ascii=False)
    return text[-200_000:]


def review_completed_turn(ctx, session_id, user_message, assistant_response,
                          conversation_history, platform, **kwargs):
    """Judge the completed turn synchronously, then queue only a justified continuation."""
    case = {
        "session_id": session_id,
        "transcript": _history_text(conversation_history),
        "cancelled": False,
        "awaiting_approval": False,
        "subagent_running": False,
        "session_active": False,
    }
    try:
        result = decide(case, live=True)
    except ReviewError as exc:
        log.warning("continuation-review Jev unavailable or invalid (%s); no injection", type(exc).__name__)
        return
    log.info("continuation-review decision=%s reason=%s confidence=%.2f", result["decision"], result["reason"], result["confidence"])
    if result["decision"] != "continue":
        return
    try:
        accepted = ctx.inject_message(CONTINUE_MESSAGE, role="user")
    except Exception as exc:
        log.warning("continuation-review injection failed (%s)", type(exc).__name__)
        return
    if not accepted:
        log.warning("continuation-review injection was refused; no continuation queued")


def register(ctx):
    ctx.register_hook("post_llm_call", lambda **kwargs: review_completed_turn(ctx, **kwargs))
