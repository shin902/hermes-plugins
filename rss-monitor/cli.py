"""Script-only RSS collection and monitor commands."""

import json
import sys

from .settings import settings


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
    ctx.register_cli_command('rss-monitor', 'Emit pending RSS batches (no network fetch)', setup, handle)
    ctx.register_cli_command('rss-collect', 'Collect all configured RSS feeds once; no LLM',
                             lambda parser: None, collect_all)
