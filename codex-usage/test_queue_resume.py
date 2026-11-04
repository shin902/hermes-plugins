"""Check queued resume forwards the exact session without new-session fallback."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import actions

sid = '01a16453-527c-76e3-b528-89b2457061e6'
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    queue = root / 'queue.json'
    state = root / 'state.json'
    queue.write_text(json.dumps([{'id': 'resume-check', 'cwd': str(root), 'prompt': 'continue', 'session_id': sid}]))
    calls = []
    def runner(prompt, cwd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout='', returncode=0, session_id=sid)
    actions.task(read=lambda: {'fiveHour': [{'windowDurationMins': 300, 'resetsAt': 19000, 'remainingPercent': 80}]}, state_path=state, queue_path=queue, now=10000, runner=runner)
    assert calls[0]['session_id'] == sid
    assert calls[0]['sandbox'] == 'read-only'
    assert json.loads(state.read_text())['taskSessionId'] == sid
    assert json.loads(queue.read_text()) == []
print('PASS: queued exact-session resume and persisted session ID')
