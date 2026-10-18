"""Profile-domain ACL for Hermes Bot Mode."""

from .policy import check
from .roster import filter_roster
from .consult import register_consult
from .tasks import register_tasks

DOMAINS = {
    "main": {"default", "coding", "computer-use", "meta-bot"},
    "local": {"local", "logbook", "screenpipe", "mail", "finance"},
}


def check_bot_acl(tool_name, args, **kwargs):
    return check(DOMAINS, "message_agent" if tool_name == "ask_bot" else tool_name, args, **kwargs)


def filter_bot_roster(request, **kwargs):
    return filter_roster(DOMAINS, request, **kwargs)


def register(ctx):
    register_consult(ctx, DOMAINS)
    register_tasks(ctx, DOMAINS)
    ctx.register_hook("pre_tool_call", check_bot_acl)
    ctx.register_middleware("llm_request", filter_bot_roster)
