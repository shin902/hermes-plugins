"""RSS-owned durable state. Native cron only schedules, monitors and runs the agent."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .settings import config_dir, home as current_home
from .monitor import EMPTY_OUTPUT, MAX_OUTPUT_CHARS, TransientFetchError, fetch_feed


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def locked(job_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
        raise ValueError('Invalid job ID')
    directory = current_home() / 'cron/rss-monitor'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f'{job_id}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory / f'{job_id}.json'


def read(path: Path, url: str | None = None) -> dict:
    state = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(state, dict) or state.get('version') != 3
            or not isinstance(state.get('seen_ids'), list)
            or not isinstance(state.get('pending'), list)
            or not isinstance(state.get('output'), str)
            or not isinstance(state.get('monitor_generation'), int)
            or (url is not None and state.get('url') != url)):
        raise ValueError('Invalid RSS state; migration required, refusing to consume articles')
    current = state.get('current')
    if current is not None and (not isinstance(current, dict) or not current.get('batch_id')
            or current.get('status') not in {'ready', 'sending', 'unknown'}
            or not isinstance(current.get('items'), list) or not current['items']):
        raise ValueError('Invalid RSS current batch')
    if (state['monitor_generation'] < 0 or isinstance(state['monitor_generation'], bool)
            or any(not isinstance(i, str) or not i for i in state['seen_ids'])):
        raise ValueError('Invalid RSS IDs/generation')
    def validate_items(items):
        for item in items:
            if (not isinstance(item, dict) or any(not isinstance(item.get(k), str)
                    for k in ('id', 'url', 'title', 'published', 'description'))
                    or not item['id'] or not re.match(r'^https?://', item['url'])):
                raise ValueError('Invalid RSS article fields')
        if len({i['id'] for i in items}) != len(items):
            raise ValueError('Duplicate RSS article IDs')
    validate_items(state['pending'])
    if current:
        if not re.fullmatch(r'[a-f0-9]{32}', current['batch_id']):
            raise ValueError('Invalid RSS batch ID')
        validate_items(current['items'])
        if {i['id'] for i in current['items']} & {i['id'] for i in state['pending']}:
            raise ValueError('RSS article appears in both current and pending')
    last = state.get('last_delivery')
    if last is not None and (not isinstance(last, dict) or last.get('status') != 'delivered'
            or not isinstance(last.get('message_ids'), list) or not last['message_ids']
            or any(not isinstance(i, str) or not i.strip() for i in last['message_ids'])):
        raise ValueError('Invalid RSS delivery receipt')
    return state


def initial(url: str, items: list[dict]) -> dict:
    return {'version': 3, 'url': url, 'seen_ids': sorted({i['id'] for i in items}),
            'pending': [], 'current': None, 'monitor_generation': 0, 'output': EMPTY_OUTPUT}


def collect(job_id: str, url: str, *, now: float | None = None) -> dict:
    at = time.time() if now is None else now
    with locked(job_id) as path:
        state = read(path, url) if path.exists() else None
        try:
            items = fetch_feed(url, diagnostics_path=current_home() / 'logs/cron-rss' / f'{job_id}.jsonl')
        except TransientFetchError as exc:
            health_path = path.with_name(f'{job_id}-health.json')
            health = json.loads(health_path.read_text()) if health_path.exists() else {}
            outage = health.get('outage') or {'first_failure_at': at, 'failures': 0, 'alert_attempted': False}
            alert = not outage['alert_attempted'] and at - outage['first_failure_at'] >= 10800
            outage.update(failures=outage['failures'] + 1, last_failure_at=at,
                          alert_attempted=outage['alert_attempted'] or alert)
            health.update(status='fetch_failed', error=str(exc), outage=outage, at=at)
            write(health_path, health)
            return {'ok': False, 'transient': True, 'alert': alert, 'error': str(exc)}
        if state is None:
            state = initial(url, items)
        else:
            seen = set(state['seen_ids'])
            state['pending'].extend(reversed([i for i in items if i['id'] not in seen]))
            state['seen_ids'] = sorted(seen | {i['id'] for i in items})
        write(path, state)
        write(path.with_name(f'{job_id}-health.json'), {'status': 'ok', 'at': at})
        return {'ok': True, 'pending': len(state['pending'])}


def excludes_shorts(job_id: str) -> bool:
    path = config_dir() / 'policies.json'
    policies = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return policies.get(job_id, {}).get('exclude_youtube_shorts') is True


def is_short(item: dict) -> bool:
    from urllib.parse import urlsplit
    url = urlsplit(item['url'])
    host = (url.hostname or '').lower()
    youtube = host == 'youtube.com' or host.endswith('.youtube.com')
    return youtube and url.path.startswith('/shorts/')


def filter_shorts(state: dict) -> bool:
    """Record policy exclusions, never pretend they are delivery receipts."""
    current = state['current']
    if current and current['status'] != 'ready':
        raise ValueError('RSS delivery unknown/partially sent; manual reconciliation required')
    excluded = [i for i in state['pending'] if is_short(i)]
    if current:
        excluded.extend(i for i in current['items'] if is_short(i))
    if not excluded:
        # Delivered snapshots are cached to suppress retries. Clear a Shorts-only
        # snapshot once on policy activation so it cannot be fed to the agent again.
        if not current and not state['pending']:
            cached = json.loads(state['output']).get('items', [])
            if cached and all(is_short(item) for item in cached):
                state['output'] = EMPTY_OUTPUT
                return True
        return False
    state['pending'] = [i for i in state['pending'] if not is_short(i)]
    if current:
        remaining = [i for i in current['items'] if not is_short(i)]
        # Invalidate any previously prepared mixed batch; stale publishers fail closed.
        state['current'] = ({'batch_id': uuid.uuid4().hex, 'status': 'ready', 'items': remaining}
                            if remaining else None)
    records = state.setdefault('policy_exclusions', {})
    for item in excluded:
        records[item['id']] = {'reason': 'youtube_shorts', 'url': item['url']}
    if not state['current'] and not state['pending']:
        state['output'] = EMPTY_OUTPUT
    return True


def prepare(job_id: str) -> str:
    """Nonce only unacknowledged batches: native pre-agent snapshots cannot lose a retry."""
    with locked(job_id) as path:
        state = read(path)
        if excludes_shorts(job_id) and filter_shorts(state):
            write(path, state)
        current = state['current']
        if current and current['status'] != 'ready':
            raise ValueError('RSS delivery unknown/partially sent; batch retained; manual reconciliation required')
        if current and len(current['items']) > 1:
            state['pending'] = current['items'] + state['pending']
            state['current'] = current = None
        if not current and state['pending']:
            batch = state['pending'][:1]
            if len(json.dumps({'items': batch}, ensure_ascii=False)) > MAX_OUTPUT_CHARS - 300:
                raise ValueError('Article exceeds RSS context limit')
            state['pending'].pop(0)
            current = {'batch_id': uuid.uuid4().hex, 'status': 'ready', 'items': batch}
            state['current'] = current
        if current:
            state['monitor_generation'] += 1
            state['output'] = json.dumps({'batch_id': current['batch_id'],
                'attempt': state['monitor_generation'], 'items': current['items']},
                ensure_ascii=False, separators=(',', ':'))
            write(path, state)
        return state['output']


def send(target: str, text: str) -> dict:
    """Public CLI; no internal send-function imports or credential extraction."""
    env = os.environ.copy()
    env['HERMES_HOME'] = str(current_home())
    result = subprocess.run(['hermes', 'send', '--to', target, '--json', '--file', '-'],
                            input=text, text=True, capture_output=True, env=env, timeout=60)
    data = json.loads(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(data.get('error') or 'Native Hermes delivery failed')
    return data


def chunks(text: str) -> list[str]:
    pieces = []
    buffer = []
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > 1800:
            pieces.append(''.join(buffer))
            buffer, units = [], 0
        buffer.append(char)
        units += size
    if buffer:
        pieces.append(''.join(buffer))
    return pieces


def publish(job_id: str, execution_id: str, batch_id: str, text: str,
            target: str, *, sender=send) -> dict:
    """LLM selects summary; code alone sends/ACKs, fencing ambiguous windows."""
    if not execution_id or not re.fullmatch(r'discord:\d+(?::\d+)?', target):
        raise ValueError('RSS publish requires an owned execution and fixed Discord destination')
    if not isinstance(text, str) or not 30 <= len(text.strip()) <= 8000:
        raise ValueError('Invalid summary size')
    if re.search(r'MEDIA:|\[\[as_document\]\]|@everyone|@here|<@', text, re.I):
        raise ValueError('RSS publish is text-only; media/mention directives are forbidden')
    with locked(job_id) as path:
        state = read(path)
        last = state.get('last_delivery')
        if last and last['batch_id'] == batch_id:
            return {'success': True, 'already_delivered': True, 'message_ids': last['message_ids']}
        current = state['current']
        if (not current or current['batch_id'] != batch_id or current['status'] != 'ready'
                or len(current['items']) != 1):
            raise ValueError('Batch not ready or wrong batch ID; refusing to resend')
        if excludes_shorts(job_id) and any(is_short(item) for item in current['items']):
            raise ValueError('YouTube Shorts publication is disabled for this job')
        if any(item['url'] not in text for item in current['items']):
            raise ValueError('Summary must include every original item URL')
        current.update(status='sending', execution_id=execution_id, target=target,
                       summary_hash=hashlib.sha256(text.encode()).hexdigest(), message_ids=[],
                       sending_started_at=time.time())
        write(path, state)  # Before the first transport side effect; survives SIGKILL.
        try:
            for part in chunks(text):
                response = sender(target, part)
                if (not isinstance(response, dict) or response.get('success') is not True
                        or not isinstance(response.get('message_id'), str) or not response['message_id'].strip()
                        or response.get('warnings')
                        or response.get('partial_success') or response.get('queued')
                        or response.get('skipped') or response.get('platform') != 'discord'):
                    raise RuntimeError('Discord delivery is not fully confirmed; batch retained')
                current['message_ids'].append(str(response['message_id']))
                write(path, state)
        except Exception as exc:
            current.update(status='unknown', delivery_error=str(exc)[:1000])
            write(path, state)
            raise
        state['last_delivery'] = {**current, 'status': 'delivered', 'delivered_at': time.time()}
        state['current'] = None
        write(path, state)
        return {'success': True, 'batch_id': batch_id, 'message_ids': current['message_ids']}
