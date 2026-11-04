"""Pre-run wake gate for differential default_session_status (stdlib only, read-only).

Hermes runs this through the ordinary cron ``--script`` hook *before* it builds the
AIAgent. Exit 0 with one JSON document:

    {"wakeAgent":false}                    no pending work -> do not launch the model
    {"wakeAgent":true,"pending":[...]}     pending work -> launch; stdout is prompt input

There is no cycle, timestamp, or other changing field: the gate runs on every
schedule tick whether or not its output changed. Failure exits nonzero, writes
``MONITOR_ERROR <component>`` to stderr, and prints no JSON at all, so a partial
payload can never be mistaken for successful work.

This is deliberately NOT Hermes monitor-script mode (``--monitor-script``), which
triggers on any stdout change and persists its hash before the model runs.

The model must call default_session_status inspect for each pending session and
pass that response's evidence_hash and full checkpoint_token to set_status. A
stale hash or token is left unacknowledged and retried on the next scheduled run.
This script never writes the checkpoint DB.
"""
import importlib.util
import json
import sys
from pathlib import Path


def _load_plugin():
    path = Path(__file__).resolve().with_name('__init__.py')
    spec = importlib.util.spec_from_file_location('session_title_status_plugin', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load session-title-status plugin')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin()


class MonitorError(RuntimeError):
    """A component failed; the caller must not treat this as an empty scan."""


def pending_work(plugin=None):
    p = plugin or PLUGIN
    try:
        checkpoints = p.read_all_checkpoints()
    except Exception as exc:
        # Missing DB is a first pass ({}); an existing unreadable/corrupt DB raises.
        raise MonitorError('checkpoint_db') from exc
    try:
        sessions, unavailable = p.collect_all_sessions()
    except Exception as exc:
        raise MonitorError('rest_list') from exc
    pending = []
    for s in sessions:
        checkpoint = checkpoints.get((s['profile'], s['id']))
        try:
            evidence = p.evidence_for_session(s['id'], s['profile'])
        except Exception as exc:
            raise MonitorError('rest_messages') from exc
        if evidence is None:
            raise MonitorError('rest_messages')
        if checkpoint is None:
            reason = 'new'
        elif checkpoint['classifier_version'] != p.CLASSIFIER_VERSION:
            reason = 'classifier_version'
        elif checkpoint['evidence_hash'] != evidence['evidence_hash']:
            reason = 'evidence_changed'
        elif checkpoint['status'] not in p.STATES:
            reason = 'status_missing'
        else:
            continue
        pending.append({'id': s['id'], 'profile': s['profile'], 'title': s.get('title'),
                        'source': s.get('source'), 'reason': reason,
                        'evidence_hash': evidence['evidence_hash'],
                        'checkpoint_token': p.checkpoint_token(checkpoint) if checkpoint else '',
                        'truncated': evidence['truncated']})
    return pending, unavailable


def wake_payload(plugin=None):
    pending, unavailable = pending_work(plugin=plugin)
    payload = {'wakeAgent': bool(pending)}
    if pending:
        payload['pending'] = pending
    if unavailable:
        payload['profiles_unavailable'] = unavailable
    return payload


def render(plugin=None):
    return json.dumps(wake_payload(plugin=plugin), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':')) + '\n'


def main(argv=None):
    if argv:
        print('usage: monitor_script.py', file=sys.stderr)
        return 2
    try:
        output = render()
    except MonitorError as exc:
        # Stable diagnostic on stderr only; no JSON is emitted on failure.
        print('MONITOR_ERROR %s' % exc, file=sys.stderr)
        return 2
    except Exception:
        # Any unexpected failure is still a failure, never an empty scan.
        print('MONITOR_ERROR internal', file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
