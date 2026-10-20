"""Canonical peer discovery for the bot_consult toolset."""
import json

from .policy import check
from .ask_bot import TOOLSET, available, identity

SCHEMA = {
    "name": "list_bot_peers",
    "description": "List canonical local bot IDs allowed by this sender's current bot-acl group. Does not start agents or send messages.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def list_peers(domains, args, **kwargs):
    try:
        source, _ = identity()
        groups = [members for members in domains.values() if source in members]
        candidates = groups[0] if len(groups) == 1 else ()
        peers = [name for name in sorted(candidates) if name != source
                 and check(domains, "message_agent", {"target": name}) is None]
        return json.dumps({"source": source, "peers": peers,
                           "modes": ["new_session", "bot_chat", "resume"]}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def register(ctx, domains):
    ctx.register_tool("list_bot_peers", TOOLSET, SCHEMA,
                      lambda args, **kw: list_peers(domains, args, **kw), check_fn=available,
                      description="List same-group bots", emoji="🔗")
