"""One-time local backfill. Never prints message bodies, summaries, IDs or credentials.

Compatibility boundary: reads the installed Hermes messages table read-only solely
for migration; normal publication and site serving do not depend on this schema.
Only confirmed delivered batch IDs can be migrated. Graph requests are GET only.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def decode(value):
    for _ in range(4):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def summaries(db):
    result = {}
    with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as conn:
        for (raw,) in conn.execute('SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL ORDER BY id'):
            calls = decode(raw)
            for call in calls if isinstance(calls, list) else []:
                call = decode(call)
                if not isinstance(call, dict):
                    continue
                function = decode(call.get('function', {}))
                if not isinstance(function, dict):
                    continue
                args = decode(function.get('arguments', {}))
                nested = args.get('calls', []) if function.get('name') == 'tool_call' and isinstance(args, dict) else []
                if function.get('name') == 'mail_publish':
                    nested = [{'name': 'mail_publish', 'arguments': args}]
                for item in nested:
                    item = decode(item)
                    if not isinstance(item, dict) or item.get('name') != 'mail_publish':
                        continue
                    params = decode(item.get('arguments'))
                    if isinstance(params, dict) and isinstance(params.get('batch_id'), str) and isinstance(params.get('summary'), str):
                        result[params['batch_id']] = params['summary']
    return result


def main():
    root = Path(__file__).parent
    spec = importlib.util.spec_from_file_location('backfill_mail', root / '__init__.py', submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pkg
    spec.loader.exec_module(pkg)
    runtime = importlib.import_module('backfill_mail.runtime')
    locked, write = runtime.locked, runtime.write
    home = Path(os.environ['HERMES_HOME'])
    with locked(home) as (_, state):
        receipts = {mid: dict(r) for mid, r in state['delivered'].items() if 'record' not in r}
    if not receipts:
        print(json.dumps({'migrated': 0, 'pending': 0}))
        return
    found = summaries(home / 'state.db')
    if any(r['batch_id'] not in found for r in receipts.values()):
        raise RuntimeError('Some confirmed summaries unavailable; no partial migration')
    result = subprocess.run(['node', str(root / 'backfill.js')], input=json.dumps(list(receipts)), text=True, capture_output=True, timeout=900)
    if result.returncode:
        print(result.stderr, file=sys.stderr)  # helper emits only fixed diagnostic fields
        raise RuntimeError('Historical Graph lookup failed')
    mails = json.loads(result.stdout)
    if set(mails) != set(receipts):
        raise RuntimeError('Incomplete lookup')
    missing = sum(bool(m.get('missing')) for m in mails.values())
    if missing:
        # Retain everything for a later operator decision instead of fabricating originals.
        print(json.dumps({'missing_originals': missing, 'migrated': 0}))
        raise RuntimeError('Originals unavailable; migration not committed')
    route_key = importlib.import_module('backfill_mail.routing').route_key
    for mid, mail in mails.items():
        mail['route_key'] = receipts[mid].get('route_key') or route_key(home, mail)
    with locked(home) as (path, state):
        write(home / 'backups/mail-sites-before-backfill.json', state)
        migrated = 0
        for mid, old in receipts.items():
            receipt = state['delivered'].get(mid)
            if receipt and receipt['batch_id'] == old['batch_id'] and 'record' not in receipt:
                mail = mails[mid]
                receipt['route_key'] = mail['route_key']
                receipt['record'] = {'mail': mail, 'summary': found[receipt['batch_id']]}
                migrated += 1
        write(path, state)
    print(json.dumps({'migrated': migrated, 'missing_originals': missing}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        import traceback
        print(json.dumps({'error_type': type(exc).__name__, 'frames': [(Path(f.filename).name, f.lineno) for f in traceback.extract_tb(exc.__traceback__)]}), file=sys.stderr)
        print('Backfill failed; private content suppressed; confirmed receipts retained', file=sys.stderr)
        sys.exit(1)
