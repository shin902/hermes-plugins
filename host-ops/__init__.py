"""Bounded host operations for the coding Hermes profile."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

HERMES = "/home/shi/.hermes/hermes-agent/.hermes/bin/hermes"
DOCKER = "/usr/bin/docker"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
CWD = "/"
UNITS = {"hermes-gateway.service", "hermes-logbook-capture.service", "llama-swap.service"}
PROFILES = {"coding", "default", "logbook", "mail", "finance", "computer-use", "meta-bot", "local"}
ENDPOINTS = {
    "logbook": "http://127.0.0.1:8788/health",
    "metro": "http://127.0.0.1:8081/status",
    "preview": "http://127.0.0.1:8082/",
    "strata": "http://127.0.0.1:8080/health",
}
ACTIONS = {
    "host_diagnostics", "health_check", "service_status", "service_logs", "service_start", "service_stop",
    "service_restart", "daemon_reload", "cron_list", "cron_status", "cron_runs", "cron_pause",
    "cron_resume", "cron_run", "config_get", "config_set", "docker_list", "docker_inspect",
}
CONFIG_TYPES = {
    "terminal.backend": (str, {"local", "docker"}),
    "terminal.docker_network": (bool, None),
    "terminal.docker_mount_cwd_to_workspace": (bool, None),
    "terminal.container_persistent": (bool, None),
    "terminal.timeout": (int, (1, 600)),
    "model.supports_vision": (bool, None),
    "agent.image_input_mode": (str, {"auto", "native", "text"}),
}
HEX_ID = re.compile(r"^[0-9a-fA-F]{8,64}$")
PROFILE_PATH = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_OUTPUT = 65536

SCHEMA = {
    "name": "host_ops",
    "description": "Run bounded diagnostics and allowlisted host service, Hermes cron/config, and Docker read-only operations. Each action accepts only its mapped params; mutations require confirm=true and report readback.",
    "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "params": {"type": "object", "description": "Parameters vary by action as listed in x-action-parameters; runtime validation enforces the exact action mapping.", "properties": {
                "endpoint": {"type": "string", "enum": sorted(ENDPOINTS)}, "unit": {"type": "string", "enum": sorted(UNITS)},
                "lines": {"type": "integer", "minimum": 1, "maximum": 300}, "confirm": {"type": "boolean"},
                "job_id": {"type": "string", "pattern": "^[0-9a-fA-F]{8,64}$"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "key": {"type": "string", "enum": sorted(CONFIG_TYPES)},
                "value": {"description": "Boolean, integer, or string validated against the selected config key.", "oneOf": [{"type": "boolean"}, {"type": "integer"}, {"type": "string"}]},
                "profile": {"type": "string", "enum": sorted(PROFILES)},
            }, "additionalProperties": False},
        }, "required": ["action", "params"], "additionalProperties": False,
        "x-action-parameters": {
            "host_diagnostics": {"allowed": []}, "health_check": {"allowed": ["endpoint"], "required": ["endpoint"]},
            "service_status": {"allowed": ["unit"], "required": ["unit"]}, "service_logs": {"allowed": ["unit", "lines"], "required": ["unit"]},
            "service_start": {"allowed": ["unit", "confirm"], "required": ["unit", "confirm"]},
            "service_stop": {"allowed": ["unit", "confirm"], "required": ["unit", "confirm"]},
            "service_restart": {"allowed": ["unit", "confirm"], "required": ["unit", "confirm"]},
            "daemon_reload": {"allowed": ["confirm"], "required": ["confirm"]},
            "cron_list": {"allowed": ["profile"]}, "cron_status": {"allowed": ["profile"]}, "cron_runs": {"allowed": ["job_id", "limit", "profile"]},
            "cron_pause": {"allowed": ["job_id", "confirm", "profile"], "required": ["job_id", "confirm"]},
            "cron_resume": {"allowed": ["job_id", "confirm", "profile"], "required": ["job_id", "confirm"]},
            "cron_run": {"allowed": ["job_id", "confirm", "profile"], "required": ["job_id", "confirm"]},
            "config_get": {"allowed": ["key"], "required": ["key"]}, "config_set": {"allowed": ["key", "value", "confirm"], "required": ["key", "value", "confirm"]},
            "docker_list": {"allowed": []}, "docker_inspect": {"allowed": ["profile"], "required": ["profile"]},
        },
    },
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect denied")


def _validate(action: Any, params: Any) -> dict[str, Any]:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unsupported action")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    allowed = SCHEMA["parameters"]["x-action-parameters"][action]["allowed"]
    if any(not isinstance(k, str) for k in params):
        raise ValueError("parameter names must be strings")
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValueError("unknown params: " + ", ".join(sorted(str(k) for k in unknown)))
    required = SCHEMA["parameters"]["x-action-parameters"][action].get("required", [])
    missing = set(required) - set(params)
    if missing:
        raise ValueError("missing params: " + ", ".join(sorted(missing)))
    p = dict(params)
    if "confirm" in p and type(p["confirm"]) is not bool:
        raise ValueError("confirm must be boolean")
    if action in {"service_start", "service_stop", "service_restart", "daemon_reload", "cron_pause", "cron_resume", "cron_run", "config_set"} and p.get("confirm") is not True:
        raise ValueError("mutation requires confirm=true")
    if "unit" in p and (not isinstance(p["unit"], str) or p["unit"] not in UNITS):
        raise ValueError("unit is not allowlisted")
    if "lines" in p and (type(p["lines"]) is not int or not 1 <= p["lines"] <= 300):
        raise ValueError("lines must be 1-300")
    if action == "health_check" and (not isinstance(p.get("endpoint"), str) or p.get("endpoint") not in ENDPOINTS):
        raise ValueError("endpoint must be a named fixed endpoint")
    if "job_id" in p and (not isinstance(p["job_id"], str) or not HEX_ID.fullmatch(p["job_id"])):
        raise ValueError("job_id must be strict hexadecimal 8-64 characters")
    if action == "cron_runs" and "limit" in p and (type(p["limit"]) is not int or not 1 <= p["limit"] <= 100):
        raise ValueError("limit must be 1-100")
    if "profile" in p and action not in {"cron_list", "cron_status", "cron_runs", "cron_pause", "cron_resume", "cron_run", "docker_inspect"}:
        raise ValueError("profile is not permitted for this action")
    if "profile" in p and (not isinstance(p["profile"], str) or p["profile"] not in PROFILES):
        raise ValueError("profile is not allowlisted")
    if action.startswith("cron_") and action not in {"cron_list", "cron_status"} and action != "cron_runs" and not p.get("job_id"):
        raise ValueError("job_id is required")
    if action in {"config_get", "config_set"}:
        key = p.get("key")
        if not isinstance(key, str) or key not in CONFIG_TYPES:
            raise ValueError("config key is not allowlisted")
        if action == "config_set":
            typ, constraint = CONFIG_TYPES[key]
            value = p.get("value")
            if type(value) is not typ:
                raise ValueError("config value has wrong type")
            if isinstance(constraint, set) and value not in constraint:
                raise ValueError("config value is outside the allowed enum")
            if isinstance(constraint, tuple) and not constraint[0] <= value <= constraint[1]:
                raise ValueError("config value is outside the allowed range")
    if action == "docker_inspect" and (not isinstance(p.get("profile"), str) or p["profile"] not in PROFILES):
        raise ValueError("profile is not allowlisted")
    return p


def _env(profile: str | None = None) -> dict[str, str]:
    # Fixed host runtime identity; intentionally do not inherit loader/python injection or credentials.
    env = {"HOME": "/home/shi", "USER": "shi", "LOGNAME": "shi", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "HERMES_HOME": "/home/shi/.hermes"}
    if profile:
        env["HERMES_PROFILE"] = profile
    return env


def _run(argv: list[str], *, timeout: float = 10, max_bytes: int = MAX_OUTPUT, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    if not argv or not all(isinstance(x, str) and "\x00" not in x for x in argv):
        raise ValueError("invalid fixed command")
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, cwd=CWD, env=env or _env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, shell=False, start_new_session=True, close_fds=True)
    buffers = {"out": bytearray(), "err": bytearray()}
    overflow = threading.Event()
    def drain(key: str, pipe) -> None:
        while True:
            chunk = pipe.read(4096)
            if not chunk: break
            room = max_bytes - len(buffers[key])
            if room > 0: buffers[key].extend(chunk[:room])
            if len(chunk) > room: overflow.set()
    readers = [threading.Thread(target=drain, args=(k, p), daemon=True) for k, p in (("out", proc.stdout), ("err", proc.stderr))]
    for thread in readers: thread.start()
    timed_out = False
    try:
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        try: os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
    for thread in readers: thread.join(max(0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in readers):
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        for pipe in (proc.stdout, proc.stderr):
            try: pipe.close()
            except OSError: pass
        for thread in readers: thread.join(0.2)
        timed_out = True
    if timed_out:
        # Descendants can retain pipe fds after parent exits. Kill the entire group and close our read ends.
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        for pipe in (proc.stdout, proc.stderr):
            try: pipe.close()
            except OSError: pass
    for thread in readers: thread.join(timeout=1)
    for pipe in (proc.stdout, proc.stderr):
        try: pipe.close()
        except OSError: pass
    if timed_out: raise TimeoutError("command timed out")
    if overflow.is_set(): raise RuntimeError("command output exceeded limit")
    return proc.returncode, bytes(buffers["out"]).decode("utf-8", "replace"), bytes(buffers["err"]).decode("utf-8", "replace")


def _hermes(profile: str, *args: str) -> tuple[int, str, str]:
    if profile not in PROFILES: raise ValueError("profile is not allowlisted")
    return _run([HERMES, "--profile", profile, *args], timeout=25, env=_env(profile))


def _status(unit: str) -> dict[str, Any]:
    rc, out, err = _run([SYSTEMCTL, "--user", "is-active", unit], timeout=5)
    state = out.strip() or err.strip()
    known = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}
    return {"ok": state in known, "unit": unit, "active": state == "active", "state": state or f"exit-{rc}"}


def _service_mutation(action: str, p: dict[str, Any]) -> dict[str, Any]:
    unit = p["unit"] if action != "daemon_reload" else None
    verb = action.removeprefix("service_") if action != "daemon_reload" else "daemon-reload"
    argv = [SYSTEMCTL, "--user", verb] + ([unit] if unit else [])
    try:
        rc, out, err = _run(argv, timeout=20)
        readback = _status(unit) if unit else {"state": "executed", "verified": False, "verification": "no independent daemon state is available"}
        intended = readback.get("ok") and ((action == "service_start" and readback.get("active")) or (action == "service_stop" and readback.get("state") == "inactive") or (action == "service_restart" and readback.get("active")))
        ok = rc == 0 and (intended if unit else rc == 0)
        return {"ok": ok, "action": action, "unit": unit, "command_exit": rc, "readback": readback,
                "verified": bool(ok and unit), "execution_state": "executed" if rc == 0 else "failed", "retry_safe": False if not ok else True, "detail": (err or out).strip()[:1000]}
    except Exception as exc:
        return {"ok": False, "action": action, "unit": unit, "error": _redact(str(exc)), "verified": False, "retry_safe": False}


def _redact(text: str) -> str:
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b(?:gho_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[REDACTED]", text)
    return re.sub(r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)\S+", r"\1\2[REDACTED]", text)


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str): return _redact(value)
    if isinstance(value, list): return [_redact_tree(item) for item in value]
    if isinstance(value, dict): return {key: _redact_tree(item) for key, item in value.items()}
    return value


def _audit(action: str, success: bool) -> None:
    try:
        path = Path(__file__).resolve().parent / "audit" / "calls.jsonl"
        path.parent.mkdir(mode=0o700, exist_ok=True); os.chmod(path.parent, 0o700)
        with path.open("a", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "action": action if action in ACTIONS else "unknown", "success": bool(success)}) + "\n")
    except OSError: pass


def _diagnostics() -> dict[str, Any]:
    result: dict[str, Any] = {"cpu": {}, "memory": {}, "gpu": {}, "processes": [], "listening_ports": []}
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.startswith(("model name", "Hardware")):
                result["cpu"][line.split(":", 1)[0]] = line.split(":", 1)[1].strip(); break
        for line in Path("/proc/meminfo").read_text().splitlines()[:5]:
            k, v = line.split(":", 1); result["memory"][k] = v.strip()
    except OSError: pass
    rc, out, _ = _run(["/usr/bin/ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"], timeout=5, max_bytes=20000)
    result["processes"] = out.splitlines()[:21]
    for path in ("/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6"):
        try:
            for line in Path(path).read_text().splitlines()[1:]:
                cols=line.split(); addr=cols[1]; state=cols[3]
                if path.endswith("tcp") or path.endswith("tcp6"):
                    if state != "0A": continue
                port=int(addr.rsplit(":",1)[1],16)
                result["listening_ports"].append({"protocol":path.rsplit("/",1)[1].split("_")[0],"port":port})
        except (OSError, ValueError, IndexError): pass
    for executable, args in (("/usr/bin/nvidia-smi", ["--query-gpu=name,memory.total,utilization.gpu", "--format=csv,noheader"]),
                             ("/opt/rocm/bin/rocminfo", [])):
        if os.path.isfile(executable):
            try:
                rc, out, err = _run([executable, *args], timeout=8, max_bytes=12000)
                result["gpu"][Path(executable).name] = {"exit":rc,"summary":(out or err).splitlines()[:30]}
            except Exception as exc: result["gpu"][Path(executable).name] = {"error":_redact(str(exc))}
    return result


def _health(name: str) -> dict[str, Any]:
    opener = build_opener(_NoRedirect)
    req = Request(ENDPOINTS[name], method="GET", headers={"Accept":"application/json,text/plain,*/*", "User-Agent":"host-ops/1"})
    try:
        with opener.open(req, timeout=3) as response:
            body = response.read(4097)
            return {"endpoint":name,"status":response.status,"ok":200 <= response.status < 300,"body":body[:4096].decode("utf-8","replace"),"truncated":len(body)>4096}
    except HTTPError as exc: return {"endpoint":name,"status":exc.code,"ok":False,"error":"HTTP error"}
    except Exception as exc: return {"endpoint":name,"ok":False,"error":_redact(type(exc).__name__ + ": " + str(exc))}


def _parse_json(rc: int, out: str, err: str) -> Any:
    if rc: raise RuntimeError(_redact(err.strip() or f"command exit {rc}"))
    return json.loads(out)


def _cron_job_readback(job_id: str, profile: str = "coding") -> dict[str, Any] | None:
    if profile not in PROFILES: raise ValueError("profile is not allowlisted")
    profile_dir = Path(f"/home/shi/.hermes/profiles/{profile}")
    if not profile_dir.is_dir(): raise ValueError("profile directory does not exist")
    path = profile_dir / "cron/jobs.json"
    if path.stat().st_size > 2_000_000:
        raise RuntimeError("cron registry exceeds read limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list): raise RuntimeError("cron registry structure is invalid")
    for job in jobs:
        if isinstance(job, dict) and job.get("id") == job_id:
            return {key:job.get(key) for key in ("id","enabled","paused","state","next_run_at","last_run_at","status","schedule","schedule_kind")}
    return None


def _docker_list() -> Any:
    rc,out,err=_run([DOCKER,"ps","--filter","label=hermes-agent=1","--format","{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],timeout=8)
    if rc: raise RuntimeError(_redact(err or "docker ps failed"))
    rows=[]
    for line in out.splitlines()[:100]:
        fields=line.split("\t",3)
        if len(fields)==4: rows.append(dict(zip(("id","name","image","status"),fields)))
    return rows


def _docker_inspect(profile: str) -> Any:
    rows=_docker_list(); matched=[]
    for row in rows:
        rc,out,err=_run([DOCKER,"inspect","--format","{{json .Config.Labels}}\t{{json .Mounts}}",row["id"]],timeout=8,max_bytes=20000)
        if rc: continue
        try:
            labels_raw,mounts_raw=out.strip().split("\t",1); labels=json.loads(labels_raw); mounts=json.loads(mounts_raw)
        except Exception: continue
        if not isinstance(labels,dict) or labels.get("hermes-agent") != "1" or labels.get("hermes-profile") != profile: continue
        row["labels"]={k:v for k,v in labels.items() if k.startswith("hermes-")}
        row["mounts"]=[{"destination":m.get("Destination"),"type":m.get("Type"),"rw":m.get("RW")} for m in mounts if isinstance(m,dict)]
        matched.append(row)
    return matched


def handle(args: dict[str, Any], **kwargs: Any) -> str:
    action = "unknown"; mutated = False
    try:
        if not isinstance(args,dict) or set(args)!={"action","params"}: raise ValueError("expected exactly action and params")
        p=_validate(args["action"],args["params"])
        action=args["action"]
        mutated = action in {"service_start","service_stop","service_restart","daemon_reload","cron_pause","cron_resume","cron_run","config_set"}
        if action == "host_diagnostics": result=_diagnostics()
        elif action == "health_check": result=_health(p["endpoint"])
        elif action == "service_status": result=_status(p["unit"])
        elif action == "service_logs":
            rc,out,err=_run([JOURNALCTL,"--user","-u",p["unit"],"-n",str(p.get("lines",100)),"--no-pager","--output","short-iso"],timeout=8)
            result={"ok":rc == 0,"unit":p["unit"],"exit":rc,"logs":_redact(out or err)}
        elif action.startswith("service_") or action=="daemon_reload": result=_service_mutation(action,p)
        elif action in {"cron_list", "cron_status", "cron_runs"}:
            profile=p.get("profile", "coding")
            if not Path(f"/home/shi/.hermes/profiles/{profile}").is_dir(): raise ValueError("profile directory does not exist")
            if action == "cron_runs":
                argv=[HERMES,"--profile",profile,"cron","runs","--limit",str(p.get("limit",20))]
                if p.get("job_id"): argv += [p["job_id"]]
                rc,out,err=_run(argv,timeout=25,env=_env(profile))
            else: rc,out,err=_hermes(profile,"cron",action.removeprefix("cron_"))
            result={"ok":rc == 0,"exit":rc,"output":_redact(out or err)}
        elif action in {"cron_pause","cron_resume","cron_run"}:
            profile=p.get("profile", "coding")
            if not Path(f"/home/shi/.hermes/profiles/{profile}").is_dir(): raise ValueError("profile directory does not exist")
            verb=action.removeprefix("cron_"); rc,out,err=_hermes(profile,"cron",verb,p["job_id"])
            if rc: raise RuntimeError(_redact(err or out or f"command exit {rc}"))
            readback=_cron_job_readback(p["job_id"], profile)
            if readback is None: raise RuntimeError("mutation command returned but durable job readback could not find target; do not retry")
            run_completed = verb == "run" and ("Ran now: succeeded." in out or "Ran now: failed." in out)
            run_queued = verb == "run" and "next scheduler tick" in out
            run_background = verb == "run" and "Running in background" in out
            verified = (verb == "pause" and readback.get("enabled") is False) or (verb == "resume" and readback.get("enabled") is True) or (verb == "run" and (run_completed or run_queued or run_background))
            # The installed CLI runs synchronously unless it explicitly reports a background dispatch or a future tick.
            result={"ok":verified,"command_output":_redact(out),"registry_readback":readback,"verified":verified,"queued":run_queued,"completed":run_completed,"running_in_background":run_background,"retry_safe":False}
        elif action == "config_get":
            rc,out,err=_hermes("coding","config","get",p["key"],"--json"); result=_parse_json(rc,out,err)
        elif action == "config_set":
            value=json.dumps(p["value"],separators=(",",":")) if isinstance(p["value"],(bool,int)) else p["value"]
            rc,out,err=_hermes("coding","config","set",p["key"],str(value))
            if rc: raise RuntimeError(_redact(err or out))
            rrc,rout,rerr=_hermes("coding","config","get",p["key"],"--json")
            if rrc: result={"ok":False,"write_succeeded":True,"verified":False,"retry_safe":False,"error":"config readback failed"}
            else:
                actual=_parse_json(rrc,rout,rerr); verified=type(actual) is type(p["value"]) and actual==p["value"]
                result={"ok":verified,"write_succeeded":True,"verified":verified,"retry_safe":False,"value":actual}
        elif action == "docker_list": result=_docker_list()
        elif action == "docker_inspect": result=_docker_inspect(p["profile"])
        else: raise ValueError("unsupported action")
        success=not (isinstance(result,dict) and result.get("ok") is False)
        _audit(action,success)
        return json.dumps(_redact_tree({"ok":success,"result":result}),ensure_ascii=False)
    except Exception as exc:
        _audit(action,False)
        return json.dumps({"ok":False,"error":_redact(type(exc).__name__+": "+str(exc)), **({"verified":False,"retry_safe":False} if mutated else {})},ensure_ascii=False)


def register(ctx: Any) -> None:
    ctx.register_tool(name="host_ops", toolset="host_ops", schema=SCHEMA, handler=handle,
                      description="Perform bounded host diagnostics and allowlisted host operations.")
