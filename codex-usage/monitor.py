#!/usr/bin/env python3
"""Read Codex rate limits without inference; emit only meaningful changes."""
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE = HOME / "plugin-data/codex-usage/state.json"
CODEX = os.environ.get("CODEX", str(Path.home() / ".local/bin/codex"))


def rpc(method, timeout=25):
    p = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    try:
        assert p.stdin and p.stdout
        def send(message):
            p.stdin.write((json.dumps(message) + "\n").encode())
            p.stdin.flush()
        def response(request_id):
            buffer = b""
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("Codex app-server closed")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        msg = json.loads(line)
                        if msg.get("id") == request_id:
                            if "error" in msg:
                                raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                            return msg["result"]
            raise TimeoutError(f"Codex app-server {method} timed out")
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hermes-codex-usage", "version": "1"}, "capabilities": {}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": method, "params": {}})
        return response(2)
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def snapshot(now=None):
    fetched = int(time.time()) if now is None else int(now)
    result = rpc("account/rateLimits/read")
    raw_pools = result.get("rateLimitsByLimitId")
    if raw_pools is None:
        one = result.get("rateLimits")
        raw_pools = {one.get("limitId") or "codex": one} if one else {}
    if not raw_pools:
        raise RuntimeError("No Codex rate-limit pools returned")
    pools, selected = {}, {"fiveHour": [], "weekly": []}
    for pool_id, pool in raw_pools.items():
        windows = {}
        for slot in ("primary", "secondary"):
            raw = pool.get(slot)
            if not raw:
                windows[slot] = None
                continue
            used, duration, reset = raw.get("usedPercent"), raw.get("windowDurationMins"), raw.get("resetsAt")
            if used is not None and (not numeric(used) or not 0 <= used <= 100):
                raise ValueError(f"Invalid usedPercent for {pool_id}.{slot}")
            if duration is not None and (not numeric(duration) or duration <= 0 or int(duration) != duration):
                raise ValueError(f"Invalid windowDurationMins for {pool_id}.{slot}")
            if reset is not None and (not numeric(reset) or reset <= 0):
                raise ValueError(f"Invalid resetsAt for {pool_id}.{slot}")
            remaining = 100 - used if used is not None else None
            reset_in = max(0, reset - fetched) if reset is not None else None
            window = {"usedPercent": used, "remainingPercent": remaining,
                      "used_percent": used, "remaining_percent": remaining,
                      "windowDurationMins": duration, "window_duration_mins": duration,
                      "resetsAt": reset, "resets_at": reset,
                      "resetInSeconds": reset_in, "reset_in_seconds": reset_in}
            windows[slot] = window
            target = "fiveHour" if duration == 300 else "weekly" if duration == 10080 else None
            if target:
                selected[target].append({"pool": pool_id, "slot": slot, **window})
        pools[pool_id] = {"plan": pool.get("planType"), **windows}
    credits = result.get("rateLimitResetCredits") or {}
    return {"source": "Codex app-server account/rateLimits/read", "fetchedAt": fetched,
            "ordinaryUsageAllowed": result.get("ordinaryUsageAllowed"), "pools": pools,
            "fiveHour": selected["fiveHour"], "weekly": selected["weekly"],
            "resetCreditsAvailable": credits.get("availableCount")}


def notify_key(s):
    def window_key(w):
        used = w.get("usedPercent")
        return {"band": int(used // 5) if numeric(used) else None,
                "windowDurationMins": w.get("windowDurationMins"), "resetsAt": w.get("resetsAt")}
    pools = {}
    for pool_id, pool in sorted(s["pools"].items()):
        pools[pool_id] = {"plan": pool.get("plan"), **{
            slot: window_key(pool[slot]) if pool.get(slot) else None for slot in ("primary", "secondary")}}
    return {"ordinaryUsageAllowed": s["ordinaryUsageAllowed"], "pools": pools,
            "fiveHour": [window_key(w) | {"pool": w["pool"], "slot": w["slot"]} for w in s["fiveHour"]],
            "weekly": [window_key(w) | {"pool": w["pool"], "slot": w["slot"]} for w in s["weekly"]],
            "resetCreditsAvailable": s["resetCreditsAvailable"]}


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".codex-usage-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(read=snapshot, state_path=STATE):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(state_path.read_text()) if state_path.exists() else None
        current = read()
        previous_key = old.get("notificationKey") if old else None
        key = notify_key(current)
        atomic_json(state_path, {"snapshot": current, "notificationKey": key})
        if previous_key != key:
            label = "Codex usage baseline" if old is None else "Codex usage or reset window changed"
            print(label + ": " + json.dumps(current, sort_keys=True))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"Codex usage monitor failed: {type(exc).__name__}: {exc}")
        sys.exit(1)
