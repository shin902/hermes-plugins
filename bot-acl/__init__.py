"""Profile-domain ACL for Hermes Bot Mode."""

from .policy import check
from .roster import filter_roster
from .ask_bot import register as register_ask_bot
from .ask_bot import register_prompt as register_consult_prompt
from .list_bot_peers import register as register_list_bot_peers
from .bot_task import register as register_bot_task

DOMAINS = {
    "main": {"default", "coding", "computer-use", "meta-bot"},
    "local": {"local", "logbook", "screenpipe", "mail", "finance"},
}


def check_bot_acl(tool_name, args, **kwargs):
    return check(DOMAINS, "message_agent" if tool_name == "ask_bot" else tool_name, args, **kwargs)


def filter_bot_roster(request, **kwargs):
    return filter_roster(DOMAINS, request, **kwargs)


def register(ctx):
    register_ask_bot(ctx, DOMAINS)
    register_list_bot_peers(ctx, DOMAINS)
    register_consult_prompt(ctx)
    register_bot_task(ctx, DOMAINS)
    ctx.register_hook("pre_tool_call", check_bot_acl)
    ctx.register_middleware("llm_request", filter_bot_roster)
