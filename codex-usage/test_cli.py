import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

root = Path(__file__).parent
spec = importlib.util.spec_from_file_location("codex_usage_cli", root / "cli.py")
assert spec and spec.loader
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
actions = cli.actions

valid = "12345678-1234-4234-8234-123456789abc"
assert actions.session_uuid(valid) == valid
for bad in ("recent", "", "1234", valid.upper()[:-1] + "Z"):
    try:
        actions.session_uuid(bad)
        raise AssertionError(f"invalid session ID accepted: {bad}")
    except ValueError:
        pass

original_run = subprocess.run
calls = []
def fake_run(argv, **kwargs):
    calls.append((argv, kwargs))
    if "resume" in argv:
        return SimpleNamespace(stdout='{"type":"thread.started","thread_id":"' + valid + '"}\n{"type":"turn.completed"}\n', returncode=0)
    return SimpleNamespace(stdout='{"type":"thread.started","thread_id":"' + valid + '"}\n{"type":"turn.completed"}\n', returncode=0)
subprocess.run = fake_run
actions.subprocess.run = fake_run
try:
    result = actions.invoke("new", Path.cwd())
    assert "--ephemeral" not in calls[-1][0] and result.session_id == valid
    resumed = actions.invoke("continue", Path.cwd(), session_id=valid, sandbox="workspace-write")
    argv = calls[-1][0]
    assert argv[argv.index("resume") + 1] == valid and resumed.session_id == valid and "--last" not in argv
    assert 'sandbox_mode="workspace-write"' in argv and "--ephemeral" not in argv
    count = len(calls)
    try:
        actions.invoke("must not run", Path.cwd(), session_id="last")
        raise AssertionError("invalid resume ID launched Codex")
    except ValueError:
        pass
    assert len(calls) == count
    def failed_resume(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.CalledProcessError(1, argv)
    actions.subprocess.run = failed_resume
    try:
        actions.invoke("no new thread", Path.cwd(), session_id=valid)
        raise AssertionError("failed resume unexpectedly succeeded")
    except subprocess.CalledProcessError:
        pass
    assert len(calls) == count + 1 and calls[-1][0][calls[-1][0].index("resume") + 1] == valid
finally:
    subprocess.run = original_run
    actions.subprocess.run = original_run

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    work = home / "work"
    work.mkdir()
    cli.STATE = home / "plugin-data/codex-usage/state.json"
    cli.actions.STATE = home / "plugin-data/codex-usage/actions.json"
    cli.QUEUE = home / "plugin-data/codex-usage/tasks.json"
    cli.actions.QUEUE = cli.QUEUE
    cli.run(cli.parser().parse_args(["tasks", "add", "--id", "one", "--cwd", str(work), "--prompt", "make file", "--model", "gpt-6.1-sol"]))
    cli.run(cli.parser().parse_args(["tasks", "add", "--id", "two", "--cwd", str(work), "--prompt", "resume later"]))
    queue = json.loads(cli.QUEUE.read_text())
    assert [x["id"] for x in queue] == ["one", "two"]
    assert cli.actions.STATE.with_suffix(".lock").exists()  # same actions.lock filename as actions.task
    try:
        cli.run(cli.parser().parse_args(["tasks", "add", "--id", "one", "--cwd", str(work), "--prompt", "duplicate"]))
        raise AssertionError("duplicate queue ID accepted")
    except ValueError:
        pass
    cli.run(cli.parser().parse_args(["tasks", "remove", "one"]))
    assert [x["id"] for x in json.loads(cli.QUEUE.read_text())] == ["two"]
    cli.STATE.write_text(json.dumps({"snapshot": {"fetchedAt": 123}}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.run(cli.parser().parse_args(["status"]))
    assert '"fetchedAt": 123' in out.getvalue()
print("PASS: exact UUID validation, persistent new session, exact resume argv/no fallback, queue add/remove/duplicate guard/shared lock, cached status")
