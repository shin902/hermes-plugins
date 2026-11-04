import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile

spec = importlib.util.spec_from_file_location("monitor", Path(__file__).with_name("monitor.py"))
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

response = {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {
    "codex": {"planType": "plus", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 2000},
              "secondary": {"usedPercent": 81, "windowDurationMins": 10080, "resetsAt": 3000}},
    "codex_other": {"planType": "plus", "primary": {"usedPercent": 37, "windowDurationMins": 60, "resetsAt": 2500},
                    "secondary": None}},
    "rateLimitResetCredits": {"availableCount": 3, "credits": [{"id": "must-not-leak"}]}}
m.rpc = lambda _: response
s = m.snapshot(now=1000)
assert len(s["fiveHour"]) == 1 and s["fiveHour"][0]["slot"] == "primary"
assert s["fiveHour"][0]["usedPercent"] == 12 and s["fiveHour"][0]["remainingPercent"] == 88
assert s["fiveHour"][0]["used_percent"] == 12 and s["fiveHour"][0]["remaining_percent"] == 88
assert s["weekly"][0]["slot"] == "secondary" and s["weekly"][0]["remainingPercent"] == 19
assert s["pools"]["codex_other"]["primary"]["windowDurationMins"] == 60
assert s["resetCreditsAvailable"] == 3 and "must-not-leak" not in json.dumps(s)

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "state.json"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.run(read=lambda: s, state_path=path)
    assert "baseline" in out.getvalue()
    first_key = json.loads(path.read_text())["notificationKey"]
    later = json.loads(json.dumps(s))
    later["fetchedAt"] += 60
    for windows in later["pools"].values():
        for slot in ("primary", "secondary"):
            if windows.get(slot) and windows[slot].get("resetInSeconds") is not None:
                windows[slot]["resetInSeconds"] -= 60
    later["fiveHour"][0]["usedPercent"] = 13
    later["fiveHour"][0]["remainingPercent"] = 87
    later["pools"]["codex"]["primary"]["usedPercent"] = 13
    later["pools"]["codex"]["primary"]["remainingPercent"] = 87
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.run(read=lambda: later, state_path=path)
    assert out.getvalue() == "" and json.loads(path.read_text())["notificationKey"] == first_key
    reset = json.loads(json.dumps(later))
    reset["fiveHour"][0]["resetsAt"] = 4000
    reset["pools"]["codex"]["primary"]["resetsAt"] = 4000
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.run(read=lambda: reset, state_path=path)
    assert "changed" in out.getvalue()
    bad = json.loads(json.dumps(response))
    bad["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 101
    m.rpc = lambda _: bad
    try:
        m.snapshot(now=1000)
        raise AssertionError("out-of-range usage accepted")
    except ValueError:
        pass
print("PASS: pool/duration mapping, used+remaining, credential-id filtering, silent countdown/12→13, reset notify, numeric validation")
