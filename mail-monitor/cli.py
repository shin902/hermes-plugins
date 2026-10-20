"""Script-only mail collection and monitor commands."""

import sys

from .settings import home


def collect_cli(args):
    from .runtime import collect, write
    try:
        result = collect(home())
        write(home()/'cron/mail-monitor/health.json',result)
        # Script-only cron stays silent even on new mail. Dispatcher handles publication.
        return 0
    except Exception:
        write(home()/'cron/mail-monitor/health.json',{'success':False,'error':'Graph collection/authentication failed; pending retained'})
        print('Mail collection failed; inspect profile health; no Discord notification',file=sys.stderr)
        return 1


def monitor_cli(args):
    from .runtime import prepare
    try: print(prepare(home())); return 0
    except Exception:
        print('Mail delivery held/monitor failed; inspect profile state',file=sys.stderr)
        return 1


def register(ctx):
    ctx.register_cli_command('mail-collect','Collect Outlook unread mail into a durable queue without LLM',lambda p:None,collect_cli)
    ctx.register_cli_command('mail-monitor','Hand one pending mail to native cron, without LLM',lambda p:None,monitor_cli)
