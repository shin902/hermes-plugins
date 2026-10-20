"""Bounded host Android/Orca operations for Hermes.

This deliberately exposes semantic actions only. In particular, no shell,
arbitrary argv, adb shell passthrough, or Orca terminal/eval surface exists.
"""
from __future__ import annotations

import json
import fcntl
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ID = "host-android"
ORCA = Path("/home/shi/.local/bin/orca")
ADB = Path("/home/shi/Android/Sdk/platform-tools/adb")
EMULATOR = Path("/home/shi/Android/Sdk/emulator/emulator")
FFMPEG_DIR = Path("/home/shi/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin")
FFMPEG = FFMPEG_DIR / "ffmpeg"
FFPROBE = FFMPEG_DIR / "ffprobe"
ANDROID_SDK = Path("/home/shi/Android/Sdk")
AVD_HOME = Path("/home/shi/.config/.android/avd")
JAVA_HOME = Path("/home/shi/.local/share/android-java")
HOME = Path("/home/shi")
PROFILE = Path("/home/shi/.hermes/profiles/coding")
EVIDENCE = PROFILE / "workspace/android-evidence"
REPOS = {
    "myjune": Path("/home/shi/ghq/github.com/samezima10101/MyJune"),
    "myjune-worktrees": Path("/home/shi/orca/workspaces/MyJune"),
    "coding": Path("/home/shi/.hermes/profiles/coding"),
}
PACKAGES = {"com.shi0203.MyJune"}
AVDS = {"MyJune_API_36"}
DEVICE_RE = re.compile(r"emulator-[0-9]+\Z")
JOB_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
MAX_OUTPUT = 1_000_000
MAX_BINARY = 256 * 1024 * 1024
TIMEOUT = 30
KEYCODES = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "menu": "KEYCODE_MENU", "enter": "KEYCODE_ENTER"}


class AndroidError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _validate_mapping(value: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise AndroidError("params must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise AndroidError("unknown parameter(s): " + ", ".join(sorted(unknown)))
    if missing:
        raise AndroidError("missing parameter(s): " + ", ".join(sorted(missing)))
    return value


def _device(value: Any) -> str:
    if not isinstance(value, str) or not DEVICE_RE.fullmatch(value):
        raise AndroidError("device must be an emulator serial (emulator-N)")
    return value


def _attach_device(value: Any) -> str:
    if isinstance(value, str) and value in AVDS:
        return value
    return _device(value)


def _package(value: Any) -> str:
    if not isinstance(value, str) or value not in PACKAGES:
        raise AndroidError("package is not in the host Android package allowlist")
    return value


def _reject_symlink_components(path: Path) -> None:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink(): raise AndroidError("symlink paths are not allowed")


def _workdir(value: Any) -> Path:
    if not isinstance(value, str):
        raise AndroidError("workdir must be an allowlisted alias or absolute directory")
    if value in REPOS:
        base = REPOS[value]
    else:
        base = Path(value)
        roots = (REPOS["myjune-worktrees"], REPOS["myjune"], REPOS["coding"])
        if not base.is_absolute() or not any(base == root or root in base.parents for root in roots):
            raise AndroidError("workdir is outside the MyJune and coding directories")
    try:
        _reject_symlink_components(base)
        resolved = base.resolve(strict=True)
    except OSError as exc:
        raise AndroidError(f"workdir is unavailable: {value}") from exc
    if resolved != base or not resolved.is_dir():
        raise AndroidError("workdir failed its allowlist check")
    return resolved


def _safe_path(value: Any, *, suffix: str, roots: tuple[Path, ...], must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AndroidError("path must be a non-empty absolute path")
    p = Path(value)
    if not p.is_absolute() or p.suffix.lower() != suffix:
        raise AndroidError(f"path must be an absolute {suffix} path")
    # Resolve existing parents and reject symlinks anywhere between the root and target.
    lexical = Path(os.path.normpath(str(p)))
    root = next((r for r in roots if lexical == r or r in lexical.parents), None)
    if root is None:
        raise AndroidError("path is outside the allowed roots")
    try:
        _reject_symlink_components(root)
        root_real = root.resolve(strict=True)
        rel = lexical.relative_to(root)
        cursor = root
        for part in rel.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AndroidError("symlink paths are not allowed")
        resolved_parent = lexical.parent.resolve(strict=must_exist)
        if resolved_parent != (root_real / rel.parent).resolve(strict=must_exist):
            raise AndroidError("path traversal is not allowed")
        final = lexical.resolve(strict=must_exist)
    except AndroidError:
        raise
    except OSError as exc:
        raise AndroidError("path cannot be resolved safely") from exc
    if root_real not in (final.parents if final != root_real else (root_real,)):
        raise AndroidError("path escaped the allowed root")
    if must_exist and (not final.is_file() or final.stat().st_size > MAX_BINARY):
        raise AndroidError("input file is missing, not regular, or exceeds the size limit")
    return final


def _clean_env(*, orca: bool = False) -> dict[str, str]:
    env = {
        "HOME": str(HOME), "PATH": f"{ORCA.parent}:{ADB.parent}:{EMULATOR.parent}:{FFMPEG_DIR}:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "ANDROID_HOME": str(ANDROID_SDK),
        "ANDROID_SDK_ROOT": str(ANDROID_SDK), "ANDROID_AVD_HOME": str(AVD_HOME), "JAVA_HOME": str(JAVA_HOME),
    }
    if orca:
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"):
            value = os.environ.get(key)
            if value and len(value) <= 4096 and "\x00" not in value:
                env[key] = value
        # These are selectors, not general-purpose environment forwarding.
        for key in ("ORCA_ENVIRONMENT", "ORCA_PAIRING_CODE"):
            value = os.environ.get(key)
            if value:
                env[key] = value
    return env


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", text)
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[redacted-github-token]", text)
    text = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b", "[redacted-api-key]", text)
    return text[:MAX_OUTPUT] + ("\n[output truncated]" if len(text) > MAX_OUTPUT else "")


def _run(argv: list[str], *, env: dict[str, str], cwd: Path | None = None, timeout: int = TIMEOUT,
         binary: bool = False, max_bytes: int = MAX_OUTPUT) -> tuple[int, bytes | str, str]:
    """Bound output during reads, drain both pipes, and terminate a timed-out group."""
    p = subprocess.Popen(argv, cwd=str(cwd) if cwd else str(PROFILE), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    out = bytearray(); err = bytearray(); overflow = False
    import selectors
    sel = selectors.DefaultSelector()
    assert p.stdout and p.stderr
    for stream, name in ((p.stdout, "out"), (p.stderr, "err")):
        os.set_blocking(stream.fileno(), False); sel.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    try:
        while sel.get_map():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in sel.select(min(0.25, max(0.01, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    sel.unregister(key.fileobj); key.fileobj.close(); continue
                target = out if key.data == "out" else err
                room = max_bytes - len(target)
                if room > 0: target.extend(chunk[:room])
                if len(chunk) > room: overflow = True
                # Continue draining without retaining excess bytes.
        code = p.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        try: os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        time.sleep(0.2)
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        raise AndroidError("host command timed out or failed while reading output")
    finally:
        for stream in (p.stdout, p.stderr):
            if stream and not stream.closed:
                try: stream.close()
                except OSError: pass
        sel.close()
    if binary and overflow: raise AndroidError("host command output exceeded the binary size limit")
    stdout: bytes | str = bytes(out) if binary else _redact(out.decode("utf-8", "replace") + ("\n[output truncated]" if overflow else ""))
    stderr = _redact(err.decode("utf-8", "replace") + ("\n[output truncated]" if overflow and len(err) >= max_bytes else ""))
    return code, stdout, stderr


def _require_ok(code: int, out: bytes | str, err: str, program: str) -> bytes | str:
    if code != 0:
        raise AndroidError(f"{program} exited {code}: {_redact(err or (out.decode('utf-8', 'replace') if isinstance(out, bytes) else out))}")
    return out


def _orca(action: str, workdir: Path, args: list[str], *, timeout: int = TIMEOUT) -> Any:
    code, out, err = _run([str(ORCA), *args, "--json"], env=_clean_env(orca=True), cwd=workdir, timeout=timeout)
    _require_ok(code, out, err, "orca")
    try: data = json.loads(out)
    except (TypeError, json.JSONDecodeError) as exc: raise AndroidError("Orca returned invalid JSON") from exc
    if isinstance(data, dict) and (data.get("ok") is False or data.get("success") is False or data.get("error")):
        raise AndroidError("Orca reported an unsuccessful operation")
    return data


def _adb(device: str, args: list[str], *, timeout: int = TIMEOUT, binary: bool = False, max_bytes: int = MAX_OUTPUT):
    code, out, err = _run([str(ADB), "-s", device, *args], env=_clean_env(), timeout=timeout, binary=binary, max_bytes=max_bytes)
    return _require_ok(code, out, err, "adb")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def _jobs_dir() -> Path:
    base = PROFILE if EVIDENCE == PROFILE or PROFILE in EVIDENCE.parents else EVIDENCE.parent
    cursor = base
    for part in EVIDENCE.relative_to(base).parts:
        cursor = cursor / part
        if cursor.is_symlink(): raise AndroidError("evidence directories may not contain symlinks")
    EVIDENCE.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = EVIDENCE / ".host-android-jobs"
    if path.is_symlink(): raise AndroidError("evidence job directory may not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700); os.chmod(path, 0o700)
    return path


def _job_path(job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id): raise AndroidError("job_id must be an opaque recording UUID")
    return _jobs_dir() / f"{job_id}.json"


def _read_job(job_id: str) -> dict:
    path = _job_path(job_id)
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > 8192: raise AndroidError("recording state is invalid")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc: raise AndroidError("recording job was not found") from exc
    fields={"id","device","duration","status","created","output","remote","pid","proc_start","completed","error","duration_verified","codec"}
    if not isinstance(data,dict) or set(data)-fields: raise AndroidError("recording state fields are invalid")
    if data.get("id") != job_id or not isinstance(data.get("device"),str) or not DEVICE_RE.fullmatch(data["device"]): raise AndroidError("recording state failed validation")
    if type(data.get("duration")) is not int or not 1<=data["duration"]<=180 or data.get("status") not in {"starting","running","complete","failed","collected"}: raise AndroidError("recording state invariants failed")
    if data.get("output") != str(EVIDENCE/job_id/"recording.mp4") or data.get("remote") != f"/sdcard/host_android_{job_id}.mp4": raise AndroidError("recording state paths failed validation")
    if type(data.get("created")) is not int: raise AndroidError("recording state timestamp is invalid")
    if "pid" in data and (type(data["pid"]) is not int or data["pid"]<=1): raise AndroidError("recording worker pid is invalid")
    if "proc_start" in data and not isinstance(data["proc_start"],str): raise AndroidError("recording worker identity is invalid")
    return data


def _proc_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return fields[21]
    except (OSError, IndexError): return None


def _worker_alive(job: dict) -> bool:
    pid = job.get("pid"); expected = job.get("proc_start")
    return isinstance(pid, int) and pid > 1 and isinstance(expected, str) and _proc_identity(pid) == expected


def _pid_exists(pid: Any) -> bool:
    if type(pid) is not int or pid <= 1: return False
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def _mark_worker_death(job: dict) -> dict:
    if job.get("status") == "starting" and int(time.time())-job["created"] > 30:
        job["status"]="failed"; job["error"]="recording worker startup timed out"
        _atomic_json(_job_path(job["id"]),job)
        if not _pid_exists(job.get("pid")): _release_device_lock(job)
    elif job.get("status") == "running" and not _worker_alive(job):
        job["status"] = "failed"; job["error"] = "recording worker exited before clean completion"
        _atomic_json(_job_path(job["id"]), job)
        _release_device_lock(job)
    return job


def _action_devices(_: dict) -> dict:
    rows = []
    adb_error = None
    try:
        code, out, err = _run([str(ADB), "devices", "-l"], env=_clean_env(), timeout=15)
        adb_text = _require_ok(code, out, err, "adb")
        for line in adb_text.splitlines()[1:]:
            fields = line.split()
            if fields and DEVICE_RE.fullmatch(fields[0]): rows.append({"serial": fields[0], "state": fields[1] if len(fields)>1 else "unknown"})
    except AndroidError as exc: adb_error = str(exc)
    orca_data = None; orca_error = None
    try: orca_data = _orca("devices", _workdir("coding"), ["emulator", "devices"])
    except AndroidError as exc: orca_error = str(exc)
    return {"adb": rows, "adb_error": adb_error, "orca": orca_data, "orca_error": orca_error,"ok":adb_error is None or orca_error is None}


def _action_avds(_: dict) -> dict:
    if not EMULATOR.is_file(): raise AndroidError("Android emulator executable is unavailable")
    code, out, err = _run([str(EMULATOR), "-list-avds"], env=_clean_env(), timeout=15)
    text = _require_ok(code, out, err, "emulator")
    return {"avds": [x for x in text.splitlines() if x in AVDS]}


def _action_attach(p: dict) -> dict:
    device = _attach_device(p["device"])
    if p["confirm"] is not True: raise AndroidError("attach may start a device; confirm=true is required")
    workdir = _workdir(p["workdir"])
    data = _orca("attach", workdir, ["emulator", "attach", device, "--worktree", "path:" + str(workdir)])
    return {"attached": True, "device": device, "result": data}


def _action_ax(p: dict) -> Any:
    device = _device(p["device"])
    workdir = _workdir(p["workdir"])
    return _orca("ax", workdir, ["emulator", "ax", "--device", device, "--worktree", "path:" + str(workdir)])


def _action_screenshot(p: dict) -> dict:
    device = _device(p["device"])
    _jobs_dir()
    root = EVIDENCE.resolve(strict=True)
    target = root / f"screenshot-{uuid.uuid4()}.png"
    if target.exists() or target.is_symlink(): raise AndroidError("evidence target already exists")
    data = _adb(device, ["exec-out", "screencap", "-p"], timeout=15, binary=True, max_bytes=MAX_BINARY)
    if not isinstance(data, bytes) or len(data) < 8 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AndroidError("device did not return a valid PNG")
    temp = root / f".screenshot-{uuid.uuid4()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, target)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass
    return {"path": str(target), "bytes": len(data)}


def _action_tap(p: dict) -> dict:
    device = _device(p["device"]); x=p["x"]; y=p["y"]
    if p["confirm"] is not True: raise AndroidError("tap changes device UI; confirm=true is required")
    if type(x) not in (int,float) or type(y) not in (int,float) or not (0 <= x <= 1 and 0 <= y <= 1): raise AndroidError("x and y must be normalized numbers from 0 to 1")
    workdir = _workdir(p["workdir"])
    result = _orca("tap", workdir, ["emulator", "tap", f"{x:.6f}", f"{y:.6f}", "--device", device, "--worktree", "path:" + str(workdir)])
    return {"tapped": True, "device": device, "result": result}


def _action_type(p: dict) -> dict:
    device = _device(p["device"]); text = p["text"]
    if not isinstance(text,str) or not re.fullmatch(r"[A-Za-z0-9 _.,!?@#%&()+:=/]{1,128}",text): raise AndroidError("text must use the safe ASCII character set and be at most 128 characters")
    if p["confirm"] is not True: raise AndroidError("typing changes device UI; confirm=true is required")
    # Orca's type command accepts a positional value; no user flags or shell parsing.
    workdir = _workdir(p["workdir"])
    result = _orca("type", workdir, ["emulator", "type", text, "--device", device, "--worktree", "path:" + str(workdir)])
    return {"typed": True, "device": device, "result": result}


def _action_key(p: dict) -> dict:
    device = _device(p["device"]); key=p["key"]
    if key not in KEYCODES: raise AndroidError("key must be back, home, menu, or enter")
    if p["confirm"] is not True: raise AndroidError("key input changes device UI; confirm=true is required")
    _adb(device, ["shell", "input", "keyevent", KEYCODES[key]], timeout=10)
    return {"sent": key, "device": device}


def _action_launch(p: dict) -> dict:
    device=_device(p["device"]); package=_package(p["package"])
    if p["confirm"] is not True: raise AndroidError("launch changes device state; confirm=true is required")
    _adb(device, ["shell", "monkey", "-p", package, "1"], timeout=20)
    return {"launched": package, "device": device}


def _action_force_stop(p: dict) -> dict:
    device=_device(p["device"]); package=_package(p["package"])
    if p["confirm"] is not True: raise AndroidError("force_stop changes device state; confirm=true is required")
    _adb(device, ["shell", "am", "force-stop", package], timeout=10)
    return {"force_stopped": package, "device": device}


def _action_install(p: dict) -> dict:
    device=_device(p["device"])
    apk=_safe_path(p["apk_path"], suffix=".apk", roots=(REPOS["myjune"], REPOS["myjune-worktrees"]))
    if p["confirm"] is not True: raise AndroidError("install changes device state; confirm=true is required")
    out=_adb(device,["install","-r",str(apk)],timeout=120,max_bytes=20000)
    return {"installed": str(apk), "output": out}


def _action_reverse(p: dict) -> dict:
    device=_device(p["device"]); port=p["port"]
    if type(port) is not int or port not in (8081,8082): raise AndroidError("port must be 8081 or 8082")
    if p["confirm"] is not True: raise AndroidError("reverse changes device networking; confirm=true is required")
    _adb(device,["reverse","tcp:"+str(port),"tcp:"+str(port)],timeout=10)
    return {"reversed": port, "device": device}


def _action_app_state(p: dict) -> dict:
    device=_device(p["device"]); package=_package(p["package"])
    out=_adb(device,["shell","dumpsys","activity","activities"],timeout=15,max_bytes=180000)
    lines=[line[:500] for line in out.splitlines() if package in line]
    return {"package":package,"matching_activity_lines":lines[:100],"truncated":len(lines)>100}


def _action_record_start(p: dict) -> dict:
    device=_device(p["device"]); duration=p["duration"]
    if type(duration) is not int or not 1 <= duration <= 180: raise AndroidError("duration must be an integer from 1 to 180 seconds")
    if p["confirm"] is not True: raise AndroidError("screen recording requires confirm=true")
    jobs=_jobs_dir()
    lock=jobs/f"device-{device}.lock"
    job_id=str(uuid.uuid4()); output_dir=EVIDENCE/job_id
    output_dir.mkdir(parents=True,mode=0o700); os.chmod(output_dir,0o700)
    state={"id":job_id,"device":device,"duration":duration,"status":"starting","created":int(time.time()),"output":str(output_dir/"recording.mp4"),"remote":f"/sdcard/host_android_{job_id}.mp4"}
    state_path=_job_path(job_id); _atomic_json(state_path,state)
    lockfd=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0),0o600)
    try:
        fcntl.flock(lockfd,fcntl.LOCK_EX)
        if os.fstat(lockfd).st_nlink != 1: raise AndroidError("recording reservation file is invalid")
        with os.fdopen(os.dup(lockfd),"r+b") as lock_stream:
            lock_stream.seek(0)
            previous=lock_stream.read().decode("ascii").strip()
            if previous:
                try:
                    stale=_read_job(previous)
                    if stale.get("status")=="running" and not _worker_alive(stale):
                        stale["status"]="failed"; stale["error"]="recording worker exited before clean completion"
                        _atomic_json(_job_path(stale["id"]),stale)
                    if stale.get("status")=="starting" and int(time.time())-stale["created"]>30:
                        stale["status"]="failed"; stale["error"]="recording worker startup timed out"
                        _atomic_json(_job_path(stale["id"]),stale)
                        if _pid_exists(stale.get("pid")): stale["status"]="running"
                except AndroidError: stale={"status":"failed"}
                if stale.get("status") in {"starting","running"}:
                    raise AndroidError("recording is already running for this device")
            lock_stream.seek(0); lock_stream.truncate(); lock_stream.write(job_id.encode("ascii")); lock_stream.flush(); os.fsync(lockfd)
    except Exception:
        os.close(lockfd)
        try: state_path.unlink(); shutil.rmtree(output_dir)
        except OSError: pass
        raise
    else:
        os.close(lockfd)
    argv=_worker_command(job_id)
    try:
        proc=subprocess.Popen(argv,cwd=str(PROFILE),env=_clean_env(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
    except OSError as exc:
        state["status"]="failed"; state["error"]="recording worker could not start"
        _atomic_json(state_path,state); _release_device_lock(state)
        try: state_path.unlink(); shutil.rmtree(output_dir)
        except OSError: pass
        return {"job_id":job_id,"status":"failed","error":state["error"],"retry_safe":True}
    state["status"]="running"; state["pid"]=proc.pid
    # start token avoids signaling or trusting a reused PID; recording worker is never killed.
    for _ in range(30):
        token=_proc_identity(proc.pid)
        if token: state["proc_start"]=token; break
        time.sleep(.01)
    if not state.get("proc_start"):
        # Leave the reservation in place: the worker may have started and still
        # be recording. Status recovery will release it only after identity dies.
        state["status"]="starting"; state["error"]="worker identity handshake pending"
    latest=_read_job(job_id)
    if latest.get("status") == "starting": _atomic_json(state_path,state)
    return {"job_id":job_id,"status":state["status"],"duration":duration,"device":device}


def _worker_command(job_id: str) -> list[str]:
    return [sys.executable,str(Path(__file__).resolve()),"--record-worker",job_id]


def _action_record_status(p: dict) -> dict:
    job=_mark_worker_death(_read_job(p["job_id"]))
    return {k:job.get(k) for k in ("id","device","duration","status","error","created","completed") if k in job}


def _valid_recording(path: Path) -> tuple[float,str]:
    code,out,err=_run([str(FFPROBE),"-v","error","-show_entries","format=duration:stream=codec_type,codec_name","-of","json",str(path)],env=_clean_env(),timeout=25,max_bytes=20000)
    raw=_require_ok(code,out,err,"ffprobe")
    try:
        data=json.loads(raw); duration=float(data["format"]["duration"])
        video=next(s for s in data.get("streams",[]) if s.get("codec_type")=="video")
    except (ValueError,KeyError,TypeError,StopIteration,json.JSONDecodeError) as exc: raise AndroidError("recording has no valid MP4 video stream") from exc
    if not 0 < duration <= 185 or not video.get("codec_name"): raise AndroidError("recording duration or codec is invalid")
    code,_,err=_run([str(FFMPEG),"-v","error","-i",str(path),"-f","null","-"],env=_clean_env(),timeout=40,max_bytes=20000)
    _require_ok(code,"",err,"ffmpeg decode verification")
    return duration,str(video["codec_name"])


def _action_record_collect(p: dict) -> dict:
    job=_mark_worker_death(_read_job(p["job_id"]))
    if job.get("status") not in {"complete","collected"}: raise AndroidError("recording is not complete; collection is unavailable")
    _device(job["device"])
    if job.get("remote") != f"/sdcard/host_android_{job['id']}.mp4": raise AndroidError("recording remote path failed ownership validation")
    target=Path(job["output"]); expected=EVIDENCE/job["id"] / "recording.mp4"
    _reject_symlink_components(target.parent)
    if target != expected or target.is_symlink(): raise AndroidError("recording output path failed ownership validation")
    if job.get("status")=="collected":
        if not target.is_file(): raise AndroidError("collected recording file is missing")
        duration,codec=_valid_recording(target)
        if duration != job.get("duration_verified") or codec != job.get("codec"): raise AndroidError("collected recording no longer matches its verified state")
        return {"path":str(target),"duration_seconds":duration,"video_codec":codec,"verified":True,"idempotent":True,"audio":"not verified; Android screenrecord evidence is silent"}
    if target.exists(): raise AndroidError("recording target already exists before collection")
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    collect_lock=_jobs_dir()/f"collect-{job['id']}.lock"
    try: lockfd=os.open(collect_lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,"O_NOFOLLOW",0),0o600)
    except FileExistsError as exc: raise AndroidError("recording collection is already in progress") from exc
    os.close(lockfd)
    temp=target.parent/f".recording-{job['id']}.mp4.part"
    try:
        if temp.exists() or temp.is_symlink(): raise AndroidError("recording temporary path already exists")
        code,out,err=_run([str(ADB),"-s",job["device"],"pull",job["remote"],str(temp)],env=_clean_env(),timeout=40,max_bytes=20000)
        _require_ok(code,out,err,"adb pull")
        if not temp.is_file() or temp.is_symlink() or temp.stat().st_size > MAX_BINARY: raise AndroidError("pulled recording is missing or exceeds size limit")
        duration,codec=_valid_recording(temp)
        os.replace(temp,target)
    except Exception:
        try: temp.unlink()
        except OSError: pass
        raise
    finally:
        try: collect_lock.unlink()
        except OSError: pass
    job["status"]="collected"; job["completed"]=int(time.time()); job["duration_verified"]=duration; job["codec"]=codec
    _atomic_json(_job_path(job["id"]),job)
    _release_device_lock(job)
    return {"path":str(target),"duration_seconds":duration,"video_codec":codec,"verified":True,"audio":"not verified; Android screenrecord evidence is silent"}


def _record_worker(job_id: str) -> int:
    path=_job_path(job_id)
    # The parent must publish the worker PID before any device operation starts.
    for _ in range(200):
        job=_read_job(job_id)
        if job.get("pid")==os.getpid() and job.get("status") in ("running","starting"): break
        if job.get("status") not in ("starting","running"): return 2
        time.sleep(.01)
    else: return 2
    if not _device(job.get("device")) or type(job.get("duration")) is not int or not 1<=job["duration"]<=180:
        return 2
    if job.get("remote") != f"/sdcard/host_android_{job_id}.mp4" or job.get("output") != str(EVIDENCE/job_id/"recording.mp4"):
        return 2
    argv=[str(ADB),"-s",job["device"],"shell","screenrecord","--time-limit",str(job["duration"]),job["remote"]]
    try:
        code,out,err=_run(argv,env=_clean_env(),timeout=job["duration"]+45,max_bytes=20000)
        if code!=0: raise AndroidError("adb screenrecord failed: "+err)
        job["status"]="complete"; job["completed"]=int(time.time())
    except Exception as exc:
        job["status"]="failed"; job["error"]=_redact(str(exc))[:1000]
    _atomic_json(path,job)
    _release_device_lock(job)
    return 0 if job["status"]=="complete" else 1


def _release_device_lock(job: dict) -> None:
    lock=_jobs_dir()/f"device-{job['device']}.lock"
    try:
        fd=os.open(lock,os.O_RDWR|getattr(os,"O_NOFOLLOW",0))
        with os.fdopen(fd,"r+b") as stream:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            current=stream.read().decode("ascii").strip()
            if current==job["id"]:
                stream.seek(0); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
    except OSError: pass


ACTIONS = {
    "devices": (set(), set(), _action_devices),
    "avds": (set(), set(), _action_avds),
    "attach": ({"device","workdir","confirm"},{"device","workdir","confirm"},_action_attach),
    "ax": ({"device","workdir"},{"device","workdir"},_action_ax),
    "screenshot": ({"device"},{"device"},_action_screenshot),
    "tap": ({"device","x","y","workdir","confirm"},{"device","x","y","workdir","confirm"},_action_tap),
    "type": ({"device","text","workdir","confirm"},{"device","text","workdir","confirm"},_action_type),
    "key": ({"device","key","confirm"},{"device","key","confirm"},_action_key),
    "launch": ({"device","package","confirm"},{"device","package","confirm"},_action_launch),
    "force_stop": ({"device","package","confirm"},{"device","package","confirm"},_action_force_stop),
    "install": ({"device","apk_path","confirm"},{"device","apk_path","confirm"},_action_install),
    "reverse": ({"device","port","confirm"},{"device","port","confirm"},_action_reverse),
    "app_state": ({"device","package"},{"device","package"},_action_app_state),
    "record_start": ({"device","duration","confirm"},{"device","duration","confirm"},_action_record_start),
    "record_status": ({"job_id"},{"job_id"},_action_record_status),
    "record_collect": ({"job_id"},{"job_id"},_action_record_collect),
}


def host_android(action: Any, params: Any) -> str:
    try:
        if not isinstance(action,str) or action not in ACTIONS: raise AndroidError("unknown action")
        allowed,required,handler=ACTIONS[action]
        p=_validate_mapping(params,allowed,required)
        result=handler(p)
        return _json({"ok":True,"action":action,"result":result})
    except AndroidError as exc:
        return _json({"ok":False,"error":_redact(str(exc))})
    except Exception as exc:
        return _json({"ok":False,"error":_redact(f"{type(exc).__name__}: {exc}")})


SCHEMA={"name":"host_android","description":"Perform one bounded Android emulator or Orca operation on the host. Use semantic actions only; no shell or arbitrary command interface. State-changing actions and screen recording require confirm=true. Orca contextual actions require an allowlisted workdir.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":sorted(ACTIONS)},"params":{"type":"object","description":"Exact action map; extra or missing fields are errors: devices{}, avds{}, attach{device,workdir,confirm}, ax{device,workdir}, screenshot{device}, tap{device,x,y,workdir,confirm}, type{device,text,workdir,confirm}, key{device,key,confirm}, launch{device,package,confirm}, force_stop{device,package,confirm}, install{device,apk_path,confirm}, reverse{device,port,confirm}, app_state{device,package}, record_start{device,duration,confirm}, record_status{job_id}, record_collect{job_id}. See README for value constraints."}},"required":["action","params"],"additionalProperties":False}}


def register(ctx: Any) -> None:
    def handler(args: Any, **_: Any) -> str:
        if not isinstance(args,dict) or set(args)!={"action","params"}:
            return _json({"ok":False,"error":"tool input must contain exactly action and params"})
        return host_android(args["action"],args["params"])
    ctx.register_tool(name="host_android",toolset="host_android",schema=SCHEMA,handler=handler)


if __name__ == "__main__" and len(sys.argv)==3 and sys.argv[1]=="--record-worker":
    if not JOB_RE.fullmatch(sys.argv[2]): raise SystemExit(2)
    raise SystemExit(_record_worker(sys.argv[2]))
