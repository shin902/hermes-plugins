import json
from pathlib import Path
import sys


def home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def settings():
    return json.loads((Path(__file__).parent/'settings.json').read_text())


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
    from .runtime import publish
    def tool(params,**kwargs):
        try:
            run = ctx.current_cron_execution()
            cfg = settings()
            if run is None or run.job_id != cfg.get('publish_job_id') or run.profile != 'mail':
                raise ValueError('mail_publish only allowed inside configured mail cron')
            result = publish(home(),params['batch_id'],params['summary'],run.execution_id,cfg['target'])
            return json.dumps(result,ensure_ascii=False)
        except Exception:
            return json.dumps({'success':False,'error':'Publication rejected/failed. Do not retry blindly; pending state retained.'})
    ctx.register_tool(name='mail_publish',toolset='mail_publish',handler=tool,schema={
        'name':'mail_publish','description':'Publish the current mail summary to its fixed Discord destination, then mark Outlook read only after confirmed send. Use current batch_id. If success=false do not blindly retry.',
        'parameters':{'type':'object','properties':{'batch_id':{'type':'string'},'summary':{'type':'string','description':'Japanese summary, max 1750 characters. No mentions or media.'}},'required':['batch_id','summary'],'additionalProperties':False}})
    ctx.register_cli_command('mail-collect','Collect Outlook unread mail into a durable queue without LLM',lambda p:None,collect_cli)
    ctx.register_cli_command('mail-monitor','Hand one pending mail to native cron, without LLM',lambda p:None,monitor_cli)
