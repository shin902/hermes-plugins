"""Confirmed mail publication tool."""

import json

from .settings import home, settings

SCHEMA = {
    'name':'mail_publish','description':'Publish the current mail summary to its fixed Discord destination, then mark Outlook read only after confirmed send. Use current batch_id. If success=false do not blindly retry.',
    'parameters':{'type':'object','properties':{'batch_id':{'type':'string'},'summary':{'type':'string','description':'Japanese summary, max 1750 characters. No mentions or media.'}},'required':['batch_id','summary'],'additionalProperties':False}}


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
    ctx.register_tool(name='mail_publish',toolset='mail_publish',handler=tool,schema=SCHEMA)
