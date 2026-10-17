"""Filter the automatic Bot Mode roster via the documented llm_request API.

This is context hygiene, not an authorization boundary. Sending remains guarded
by policy.check. No core roster/name-resolution helpers are imported.
"""

import copy
import logging
import re
from functools import lru_cache
from pathlib import Path

from .policy import check

logger = logging.getLogger(__name__)
# Stop at the capability stamp as well as a heading: platform instructions may
# follow the stamp WITHOUT a Markdown heading and must never be removed.
_SECTION = re.compile(r"(?ms)^## Messaging other agents\r?\n.*?(?=^Capability epoch: |^## |\Z)")
_ROSTER = re.compile(r"(?m)^You are `@[^`\r\n]+`\. (?:Your teammates|Allowed local teammates)[^\r\n]*:\r?\n")
_BULLET = re.compile(r"^- `@([a-z0-9][a-z0-9_-]{0,63})`(?: — .*|\s*)$")
_REMOTE = re.compile(r"(?ms)^Teammates on OTHER connected machines[^\r\n]*:\r?\n(?:- [^\r\n]*(?:\r?\n|\Z))*")
_PEERS = re.compile(r"(?m)^Teammates on OTHER machines:[^\r\n]*(?:\r?\n|\Z)")


@lru_cache(maxsize=512)
def _visible_ids(home_text, session_id, domain_snapshot):
    # Freeze the allowed roster per session/domain configuration to avoid
    # per-turn prompt-prefix drift. Plugin reload explicitly resets this cache.
    domains = {name: set(members) for name, members in domain_snapshot}
    home = Path(home_text)
    source = home.name if home.parent.name == "profiles" else "default"
    groups = [members for members in domains.values() if source in members]
    if len(groups) != 1:
        return source, ()
    allowed = tuple(name for name in sorted(groups[0])
                    if name != source and check(domains, "message_agent", {"target": name}) is None)
    return source, allowed


def _replace_section(match, source, visible):
    section = match.group()
    roster = _ROSTER.search(section)
    heading = section[:section.index("\n") + 1]
    # Preserve built-in transport and privacy instructions, not its roster.
    prefix = section[len(heading):roster.start()] if roster else (
        "Use message_agent only for the local teammates listed below. "
        "Delivery is asynchronous; compose your own message and follow its acknowledgement.\n"
    )
    if not roster:
        logger.warning("bot-acl: automatic roster format changed; replaced the entire recognized section")
    roles = {}
    tail = ""
    if roster:
        remaining = section[roster.end():].splitlines(keepends=True)
        consumed = 0
        for line in remaining:
            bullet = _BULLET.fullmatch(line.rstrip("\r\n"))
            if bullet:
                roles.setdefault(bullet[1], line.rstrip("\r\n"))
            elif line.rstrip("\r\n") != "- (no teammates yet)":
                break
            consumed += 1
        # Remove only the recognized remote/peer roster addenda. Unrelated
        # instructions after the local bullets must survive unchanged.
        tail = _PEERS.sub("", _REMOTE.sub("", "".join(remaining[consumed:])))
    lines = []
    for name in visible:
        # Canonical default is emitted as @default, not an arbitrary display alias.
        original = roles.get(name) or (roles.get("hermes") if name == "default" else None)
        suffix = original.partition(" — ")[2] if original else ""
        lines.append(f"- `@{name}`" + (f" — {suffix}" if suffix else ""))
    return (heading + prefix + f"You are `@{source}`. Allowed local teammates (same bot-acl group):\n"
            + ("\n".join(lines) if lines else "- (no permitted teammates)") + "\n" + tail)


def filter_roster(domains, request, **kwargs):
    updated = copy.deepcopy(request)
    scope = None

    def rewrite(value):
        nonlocal scope
        if isinstance(value, str):
            if not _SECTION.search(value):
                return value
            if scope is None:
                try:
                    from hermes_constants import get_hermes_home
                    snapshot = tuple(sorted((name, tuple(sorted(members))) for name, members in domains.items()))
                    scope = _visible_ids(str(get_hermes_home()), str(kwargs.get("session_id") or ""), snapshot)
                except Exception:
                    # Do not throw into the host's fail-open middleware chain.
                    logger.exception("bot-acl: roster scope unavailable; hiding all automatic teammates")
                    scope = ("unknown", ())
            assert scope is not None
            return _SECTION.sub(lambda match: _replace_section(match, *scope), value)
        if isinstance(value, list):
            return [rewrite(part) for part in value]
        if isinstance(value, dict):
            result = dict(value)
            for key in ("text", "content"):
                if key in result:
                    result[key] = rewrite(result[key])
            return result
        return value

    # Provider-neutral handling of Chat Completions and Responses payloads.
    if isinstance(updated.get("instructions"), (str, list)):
        updated["instructions"] = rewrite(updated["instructions"])
    for key in ("messages", "input"):
        if not isinstance(updated.get(key), list):
            continue
        for message in updated[key]:
            if isinstance(message, dict) and message.get("role") in ("system", "developer"):
                message["content"] = rewrite(message.get("content"))
    if updated == request:
        return None
    return {"request": updated, "source": "bot-acl", "reason": "hide automatic teammates outside the allowed local group"}
