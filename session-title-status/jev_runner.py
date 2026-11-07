"""TypeSafe Jev status classifier, run as a Hermes ``no_agent`` cron script.

Per tick: pending sessions of cloud-allowed profiles only (monitor_script scope
``cloud``) -> ``inspect`` -> one Jev ``choice`` question -> ``set_status`` with that
inspect's evidence_hash and checkpoint_token. Never calls set_title.

no_agent contract: stdout is delivered verbatim (empty = silent success), nonzero
exit is an error alert. Success prints one ``<profile>: N changed`` line per profile
with changed titles. Failures print ``JEV_ERROR <profile> <session> <class>`` lines
(no content, no key) to stderr and exit 1; other sessions still run. A failed
session gets no set_status, so it stays pending for the next tick.
``skipped``/``retry``/``checkpoint conflict`` results are normal and left for the
next tick.

Privacy: ``is_cloud_allowed`` (bot-acl ``local`` group, fail closed) selects the
sessions AND is re-checked immediately before every network send. Local-group
sessions are never sent to Jev, in dry-run too.

MIN_CONFIDENCE = 0.30. Jev confidence is how concentrated the probabilities are
(1 = one option, 0 = uniform). Below it the answer is not trusted and the status is
🔴, the existing "no clearer state can be established" meaning. Calibrated on 40
recent default/coding sessions (dry-run): confidence did not separate agreement
with the local model from disagreement (median 0.65 vs 0.60), so the threshold is
deliberately low and only catches near-uniform answers (roughly 7-10% of sessions
at 0.30; 0.50 would turn about a third red).

``--dry-run`` does everything except set_status. ``--recent N`` (dry-run only)
classifies the N newest eligible sessions regardless of checkpoint, for calibration;
``--profile`` (repeatable) limits the profiles.
"""
import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
MIN_CONFIDENCE = 0.30
QUESTION_ID = 'status'
# Stable ASCII option keys -> status emoji.
OPTIONS = {'unresolved': '🔴', 'outbox': '📤', 'review': '👀', 'running': '⏳', 'done': '✅'}
QUESTION = {
    'type': 'choice',
    'instructions': (
        'Pick the single best status for this session from its latest messages (oldest to newest). '
        'The title is only a human label; a leading status emoji in it is not evidence. '
        'The newest unresolved request wins over an older completion: if anything newer than the '
        'last completion is still open, never choose done. An assistant claiming completion does '
        'not override a pending check, an unanswered user request, or an error. Choose running only '
        'when work is genuinely in progress, never for a promise, plan, acknowledgement or mere '
        'waiting. If no clearer state can be established, choose unresolved.'),
    'criteria': {
        'unresolved': 'A blocker, error or failure, an open user request that nobody has answered '
                      'yet, or ambiguous or insufficient evidence. Use only when no clearer state can '
                      'be established.',
        'outbox': 'The evidence explicitly shows a completed draft or message awaiting sending.',
        'review': 'The assistant reported or delivered a result, change or question, and the work now '
                  'awaits the user\'s validation, review, confirmation or next decision.',
        'running': 'Work is genuinely running or in progress right now, not a promise, plan, '
                   'acknowledgement or mere waiting.',
        'done': 'Explicitly complete: nothing newer is unresolved and no check is pending.',
    },
}


def _load_sibling(name):
    path = Path(__file__).resolve().with_name(name)
    spec = importlib.util.spec_from_file_location('session_title_status_' + path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load ' + name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = _load_sibling('monitor_script.py')
PLUGIN = monitor.PLUGIN


class JevError(ValueError):
    """Missing key, transport failure, malformed response, or a privacy refusal."""


def _jev_key():
    """TYPESAFE_API_KEY from the environment, else parsed (never sourced) from <root>/.env."""
    key = os.environ.get('TYPESAFE_API_KEY')
    if key:
        return key
    try:
        text = (PLUGIN._root() / '.env').read_text(encoding='utf-8')
    except OSError:
        return None
    for line in text.splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*?)\s*', line)
        if match:
            return match.group(1).strip('"\'') or None
    return None


def post_jev(payload, key):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(body) > 32 * 1024 * 1024:
        raise JevError('request too large')
    req = urllib.request.Request(API_URL, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise JevError('response too large')
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise JevError('HTTP %d' % exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, JevError):
            raise
        raise JevError('request failed: ' + type(exc).__name__)


def human_title(title):
    """Title without its leading status emoji: the emoji is not evidence."""
    for emoji in PLUGIN.STATES:
        if title.startswith(emoji):
            return title[len(emoji):].lstrip()
    return title


def build_payload(seen):
    state = {'title': human_title(seen['title']), 'truncated': seen['truncated'],
             'messages': seen['messages']}
    return {'model': MODEL, 'state': state, 'questions': {QUESTION_ID: QUESTION}}


def parse_response(response):
    try:
        answer = response['answers'][QUESTION_ID]
        choice, probabilities, confidence = answer['choice'], answer['probabilities'], answer['confidence']
    except (KeyError, TypeError):
        raise JevError('response missing choice fields')
    if answer.get('type') != 'choice' or choice not in OPTIONS:
        raise JevError('unsupported choice')
    if not isinstance(probabilities, dict) or set(probabilities) != set(OPTIONS):
        raise JevError('probabilities do not match options')
    values = list(probabilities.values())
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= 1
           for x in values):
        raise JevError('invalid probabilities')
    if abs(sum(values) - 1.0) > 0.02:
        raise JevError('probabilities do not sum to one')
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise JevError('invalid confidence')
    return choice, float(confidence), probabilities


def classify(seen, transport):
    """(emoji, confidence, probabilities, jev_choice) for one inspect result."""
    choice, confidence, probabilities = parse_response(transport(build_payload(seen)))
    emoji = OPTIONS[choice] if confidence >= MIN_CONFIDENCE else OPTIONS['unresolved']
    return emoji, confidence, probabilities, choice


def _candidates(scope_profiles, recent, now):
    """Pending cloud work, or (calibration) the N newest eligible sessions."""
    if recent is None:
        pending, _ = monitor.pending_work(plugin=PLUGIN, now=now, scope='cloud')
        return [s for s in pending if not scope_profiles or s['profile'] in scope_profiles]
    found = []
    for profile in scope_profiles or list(PLUGIN.PROFILES):
        if not PLUGIN.is_cloud_allowed(profile):
            continue
        try:
            sessions, _ = PLUGIN.collect_all_sessions(profile)
        except Exception as exc:
            raise monitor.MonitorError('rest_list') from exc
        found += sessions
    found.sort(key=lambda s: monitor._epoch_seconds(s.get('last_active')) or 0, reverse=True)
    return found[:recent]


def run(dry_run=False, recent=None, profiles=None, transport=None, now=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    now = time.time() if now is None else now
    try:
        work = _candidates(profiles, recent, now)
    except monitor.MonitorError as exc:
        print('MONITOR_ERROR %s' % exc, file=err)
        return 2
    if not work:
        return 0
    if transport is None:
        key = _jev_key()
        if not key:
            print('JEV_ERROR - - KeyMissing', file=err)
            return 1
        transport = lambda payload: post_jev(payload, key)
    changed, failures = {}, []
    for item in work:
        profile, sid = item['profile'], item['id']
        try:
            seen = PLUGIN.perform({'operation': 'inspect', 'profile': profile, 'session_id': sid})
            if 'skipped' in seen:
                continue
            # Defense in depth: re-check the partition right before any network send.
            if not PLUGIN.is_cloud_allowed(profile):
                raise JevError('privacy refusal')
            emoji, confidence, probabilities, choice = classify(seen, transport)
            if dry_run:
                print(json.dumps({'profile': profile, 'session_id': sid, 'title': seen['title'],
                                  'chosen': emoji, 'jev_choice': choice, 'confidence': confidence,
                                  'probabilities': probabilities}, ensure_ascii=False), file=out)
                continue
            result = PLUGIN.perform({'operation': 'set_status', 'profile': profile, 'session_id': sid,
                                     'expected_title': seen['title'], 'status': emoji,
                                     'evidence_hash': seen['evidence_hash'],
                                     'checkpoint_token': seen['checkpoint_token']})
            if result.get('acknowledged') and result.get('updated'):
                changed[profile] = changed.get(profile, 0) + 1
        except Exception as exc:
            failures.append((profile, sid, type(exc).__name__))
    for profile in sorted(changed):
        print('%s: %d changed' % (profile, changed[profile]), file=out)
    for profile, sid, name in failures:
        print('JEV_ERROR %s %s %s' % (profile, sid, name), file=err)
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Classify cloud-allowed sessions with Jev.')
    parser.add_argument('--dry-run', action='store_true', help='classify and print; never set_status')
    parser.add_argument('--recent', type=int, metavar='N',
                        help='calibration: N newest eligible sessions regardless of checkpoint (dry-run)')
    parser.add_argument('--profile', action='append', choices=list(PLUGIN.PROFILES),
                        help='limit to this profile (repeatable)')
    args = parser.parse_args(argv)
    if args.recent is not None and (args.recent < 1 or not args.dry_run):
        parser.error('--recent needs N >= 1 and --dry-run')
    try:
        return run(dry_run=args.dry_run, recent=args.recent, profiles=args.profile)
    except Exception:
        print('JEV_ERROR internal', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
