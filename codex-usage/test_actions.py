import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

root = Path(__file__).parent
spec = importlib.util.spec_from_file_location("actions", root / "actions.py")
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

raw = {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {
    "codex": {"planType": "plus", "primary": {"usedPercent": 5, "windowDurationMins": 60, "resetsAt": 10000},
              "secondary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 20000}},
    "codex_other": {"planType": "plus", "primary": {"usedPercent": 30, "windowDurationMins": 10080, "resetsAt": 30000}, "secondary": None}}}
original_rpc = m.monitor.rpc
m.monitor.rpc = lambda _: raw
mapped = m.monitor.snapshot(now=1000)
m.monitor.rpc = original_rpc
assert mapped["fiveHour"][0]["slot"] == "secondary"
assert mapped["fiveHour"][0]["used_percent"] == 20 and mapped["fiveHour"][0]["remaining_percent"] == 80
assert mapped["weekly"][0]["pool"] == "codex_other" and mapped["weekly"][0]["slot"] == "primary"


def usage(remaining=80, reset=20000):
    return {"fiveHour": [{"pool": "codex", "slot": "secondary", "windowDurationMins": 300,
                           "resetsAt": reset, "remainingPercent": remaining}]}

start = 20000 - 150 * 60
assert not m.eligible(m.active_five_hour(usage()), start - 1)
assert m.eligible(m.active_five_hour(usage()), start)
assert not m.eligible(m.active_five_hour(usage(79)), start)
assert m.eligible(m.active_five_hour(usage(80)), start)
assert not m.eligible(m.active_five_hour(usage(101)), start)
assert not m.eligible(None, start)
assert "private prompt" not in m.error_summary(__import__("subprocess").CalledProcessError(1, ["codex", "exec", "private prompt"]))

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    state, queue = home / "state.json", home / "tasks.json"
    queue.write_text("[]\n")
    m.task(read=lambda: usage(), state_path=state, queue_path=queue, now=start,
           runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    assert json.loads(queue.read_text()) == [] and not state.exists()
    cwd = home / "work"
    cwd.mkdir()
    task_obj = {"id": "fixture", "cwd": str(cwd), "prompt": "create file"}
    queue.write_text(json.dumps([task_obj]))
    calls = []
    def fake(prompt, path, **kwargs):
        calls.append((prompt, path, kwargs))
        return SimpleNamespace(stdout='{"type":"turn.completed"}', returncode=0, session_id="12345678-1234-4234-8234-123456789abc")
    m.task(read=lambda: usage(80), state_path=state, queue_path=queue, now=start, runner=fake)
    saved = json.loads(state.read_text())
    assert saved["taskStatus"] == "completed" and saved["taskId"] == "fixture"
    assert saved["taskSessionId"] == "12345678-1234-4234-8234-123456789abc"
    assert saved["taskHistory"] == [{"id": "fixture", "resetAt": 20000, "cwd": str(cwd), "status": "completed", "exitCode": 0, "sessionId": "12345678-1234-4234-8234-123456789abc"}]
    assert len(calls) == 1 and json.loads(queue.read_text()) == []
    m.task(read=lambda: usage(80), state_path=state, queue_path=queue, now=start, runner=fake)
    assert len(calls) == 1
    queue.write_text(json.dumps([{"id": "next", "cwd": str(cwd), "prompt": "next task"}]))
    m.task(read=lambda: usage(80), state_path=state, queue_path=queue, now=start, runner=fake)
    assert len(calls) == 2 and json.loads(queue.read_text()) == [] and len(json.loads(state.read_text())["taskHistory"]) == 2
    queue.write_text(json.dumps([{"id": "uncertain", "cwd": str(cwd), "prompt": "x"}]))
    def fail(*_args, **_kwargs): raise TimeoutError("ambiguous timeout")
    try:
        m.task(read=lambda: usage(80, reset=40000), state_path=state, queue_path=queue,
               now=40000 - 150 * 60, runner=fail)
    except TimeoutError:
        pass
    assert json.loads(state.read_text())["taskStatus"] == "unknown; no automatic relaunch"
    assert json.loads(state.read_text())["task"]["prompt"] == "x"
    count = len(json.loads(queue.read_text()))
    m.task(read=lambda: usage(80, reset=60000), state_path=state, queue_path=queue,
           now=60000 - 150 * 60, runner=fake)
    assert len(json.loads(queue.read_text())) == count
    queue.write_text(json.dumps([{"id": "kept", "cwd": str(cwd), "prompt": "x"}]))
    try:
        m.task(read=lambda: (_ for _ in ()).throw(RuntimeError("offline")), state_path=state,
               queue_path=queue, now=start, runner=fake)
    except RuntimeError:
        pass
    assert len(json.loads(queue.read_text())) == 1

    ps = home / "prime.json"
    primes = []
    m.prime(read=lambda: usage(reset=20000), state_path=ps, now=19999, sender=lambda: primes.append("early"))
    assert not primes
    reads = iter([usage(reset=20000), usage(reset=38000)])
    m.prime(read=lambda: next(reads), state_path=ps, now=20000, sender=lambda: primes.append("hello"))
    assert primes == ["hello"] and json.loads(ps.read_text())["primeStatus"] == "verified"
    m.prime(read=lambda: usage(reset=20000), state_path=ps, now=20001, sender=lambda: primes.append("duplicate"))
    assert primes == ["hello"]

    # A fresh installation records its current future deadline without spending usage.
    fresh_state = home / "fresh-prime.json"
    unused = {"fiveHour": [{"windowDurationMins": 300, "resetsAt": 38000,
                             "usedPercent": 0, "remainingPercent": 100}]}
    m.prime(read=lambda: unused, state_path=fresh_state, now=20000,
            sender=lambda: primes.append("fresh-start"))
    assert primes == ["hello"] and json.loads(fresh_state.read_text()) == {"observedResetAt": "38000"}

    # A future deadline with usage is already-active usage, not a reset to prime.
    active = {"fiveHour": [{"windowDurationMins": 300, "resetsAt": 38000,
                             "usedPercent": 18, "remainingPercent": 82}]}
    m.prime(read=lambda: active, state_path=fresh_state, now=20001,
            sender=lambda: primes.append("active-window"))
    assert primes == ["hello"] and json.loads(fresh_state.read_text())["observedResetAt"] == "38000"

    # Track the old deadline on a pre-reset poll, then detect rollover after it expires.
    rollover_state = home / "rollover-prime.json"
    old_window = {"fiveHour": [{"windowDurationMins": 300, "resetsAt": 20000,
                                 "usedPercent": 18, "remainingPercent": 82}]}
    calls = []
    m.prime(read=lambda: old_window, state_path=rollover_state, now=19999,
            sender=lambda: calls.append("early"))
    assert calls == [] and json.loads(rollover_state.read_text()) == {"observedResetAt": "20000"}
    reads = iter([unused, {"fiveHour": [{"windowDurationMins": 300, "resetsAt": 38000,
                                          "usedPercent": 0, "remainingPercent": 100}]}])
    m.prime(read=lambda: next(reads), state_path=rollover_state, now=20001,
            sender=lambda: calls.append("hello"))
    rolled = json.loads(rollover_state.read_text())
    assert calls == ["hello"] and rolled["primeAttemptedFor"] == "20000"
    assert rolled["observedResetAt"] == "38000" and rolled["primeStatus"] == "verified"
    m.prime(read=lambda: unused, state_path=rollover_state, now=20002,
            sender=lambda: calls.append("duplicate"))
    assert calls == ["hello"]

    failed_read_state = home / "failed-read-prime.json"
    try:
        m.prime(read=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                state_path=failed_read_state, now=20001,
                sender=lambda: calls.append("must-not-send"))
        raise AssertionError("prime ignored a failed usage read")
    except RuntimeError as exc:
        assert str(exc) == "offline"
    assert not failed_read_state.exists() and calls == ["hello"]

    uncertain_state = home / "uncertain-prime.json"
    def ambiguous_send():
        raise TimeoutError("send outcome unknown")
    try:
        m.prime(read=lambda: usage(reset=20000), state_path=uncertain_state,
                now=20000, sender=ambiguous_send)
        raise AssertionError("ambiguous prime unexpectedly succeeded")
    except TimeoutError:
        pass
    uncertain = json.loads(uncertain_state.read_text())
    assert uncertain["primeAttemptedFor"] == "20000"
    assert uncertain["primeStatus"] == "uncertain; no automatic retry"
    m.prime(read=lambda: usage(reset=20000), state_path=uncertain_state, now=20001,
            sender=lambda: calls.append("retry"))
    assert calls == ["hello"]
print("PASS: elapsed 149/150m, remaining 79/80/101%, empty queue, one claim, unknown fence/prompt retention, failed-read, expired-reset hello+fresh verification+dedupe, rollover, active-window skip, fresh baseline, uncertain-send fence")
