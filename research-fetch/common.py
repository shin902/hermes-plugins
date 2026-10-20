"""Shared research CLI execution."""

import json

CLI = '/workspace/research-cli/research_cli.py'
TIMEOUT = 90


def text(value, key):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000 or '\x00' in value:
        raise ValueError(f'{key} must be a nonempty string (max 4000 characters, no NUL)')
    return value


def run(ctx, args, build):
    try:
        result = ctx.dispatch_tool('terminal', {'command': build(args), 'timeout': TIMEOUT})
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise ValueError('terminal returned a non-object response')
        if result.get('error') or result.get('exit_code') != 0:
            return json.dumps({'error': result.get('error') or result.get('output') or 'terminal failed', 'exit_code': result.get('exit_code')}, ensure_ascii=False)
        output = result.get('output')
        if not isinstance(output, str):
            raise ValueError('terminal response missing output')
        return json.dumps({'markdown': output}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'error': str(exc)}, ensure_ascii=False)
