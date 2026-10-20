"""Confirmed RSS publication tool."""

import json

from .settings import settings

SCHEMA = {'name': 'rss_publish',
        'description': 'Publish the verified summary of the current RSS batch to its fixed Discord destination. Code acknowledges only confirmed delivery. Never blindly retry an unknown/partial delivery.',
        'parameters': {'type': 'object', 'properties': {
            'batch_id': {'type': 'string'}, 'summary': {'type': 'string'}},
            'required': ['batch_id', 'summary'], 'additionalProperties': False}}


def register(ctx):
    from .runtime import publish
    def publish_tool(params, **kwargs):
        try:
            run = ctx.current_cron_execution()
            feeds, targets = settings()
            if run is None or run.job_id not in feeds:
                raise ValueError('rss_publish is limited to configured RSS cron executions')
            return json.dumps(publish(run.job_id, run.execution_id,
                                      params['batch_id'], params['summary'], targets[run.job_id]),
                              ensure_ascii=False)
        except Exception as exc:
            return json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False)
    ctx.register_tool(name='rss_publish', toolset='rss_publish', handler=publish_tool, schema=SCHEMA)
