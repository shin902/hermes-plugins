"""RSS pipeline on public Hermes plugin/CLI interfaces. No scheduler monkey patches."""
import json
from pathlib import Path
import sys


def home():
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def config_dir():
    return Path(home()) / 'plugin-config/rss-monitor'


def settings():
    directory = config_dir()
    feeds = json.loads((directory / 'feeds.json').read_text(encoding='utf-8'))
    targets = json.loads((directory / 'delivery.json').read_text(encoding='utf-8'))
    if set(feeds) != set(targets):
        raise ValueError('RSS feeds/delivery jobs differ')
    return feeds, targets


def setup(parser):
    parser.add_argument('--job-id', required=True)


def handle(args):
    from .runtime import prepare
    try:
        feeds, _ = settings()
        if args.job_id not in feeds:
            raise ValueError('Unknown RSS job')
        print(prepare(args.job_id))
    except Exception as exc:
        print(f'RSS monitor failed: {exc}', file=sys.stderr)
        return 1
    return 0


def collect_all(args):
    from .runtime import collect, send
    try:
        feeds, targets = settings()
    except Exception as exc:
        print(f'RSS settings failed: {exc}', file=sys.stderr)
        return 1
    errors = []
    for job_id, url in feeds.items():
        try:
            result = collect(job_id, url)
            if not result['ok']:
                # A failed feed remains failed in plugin health + HTTP diagnostics, never read.
                # The collector's task is to perform/record attempts, not deliver articles.
                print(f"RSS fetch failed {job_id}: {result['error']}", file=sys.stderr)
            if result.get('alert'):
                from .runtime import locked, write
                alert_result = {'status': 'unknown', 'error': 'Notification interrupted'}
                try:
                    response = send(targets[job_id],
                        f"⚠ RSS取得が3時間以上継続して失敗しています。job={job_id}\n{result['error']}")
                    if (response.get('success') is not True or not isinstance(response.get('message_id'), str)
                            or not response['message_id'].strip()):
                        raise RuntimeError('Outage notification failed')
                    alert_result = {'status': 'delivered', 'message_id': response['message_id']}
                except Exception as exc:
                    alert_result = {'status': 'unknown', 'error': str(exc)[:1000]}
                    raise
                finally:
                    # One ATTEMPT per outage, not guaranteed delivery or an automatic retry.
                    # Uncertain sends are never blindly repeated; native failure lane reports errors.
                    with locked(job_id) as state_path:
                        health_path = state_path.with_name(f'{job_id}-health.json')
                        health = json.loads(health_path.read_text())
                        health['outage']['notification'] = alert_result
                        write(health_path, health)
        except Exception as exc:
            errors.append(job_id)
            print(f'RSS collection/config/state/notification error {job_id}: {exc}', file=sys.stderr)
    # Native failure lane alerts on configuration/state/notification failures only.
    return 1 if errors else 0


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
    ctx.register_tool(name='rss_publish', toolset='rss_publish', handler=publish_tool,
        schema={'name': 'rss_publish',
                'description': 'Publish the verified summary of the current RSS batch to its fixed Discord destination. Code acknowledges only confirmed delivery. Never blindly retry an unknown/partial delivery.',
                'parameters': {'type': 'object', 'properties': {
                    'batch_id': {'type': 'string'}, 'summary': {'type': 'string'}},
                    'required': ['batch_id', 'summary'], 'additionalProperties': False}})
    ctx.register_cli_command('rss-monitor', 'Emit pending RSS batches (no network fetch)', setup, handle)
    ctx.register_cli_command('rss-collect', 'Collect all configured RSS feeds once; no LLM',
                             lambda parser: None, collect_all)
