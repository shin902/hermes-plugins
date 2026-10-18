import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("host_android_plugin", PLUGIN)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def decode(value):
    return json.loads(value)


class ValidationTests(unittest.TestCase):
    def test_unknown_action_and_fields_rejected(self):
        self.assertFalse(decode(plugin.host_android("exec", {}))["ok"])
        self.assertFalse(decode(plugin.host_android("devices", {"extra": 1}))["ok"])
        self.assertFalse(decode(plugin.host_android("devices", None))["ok"])

    def test_registers_only_one_tool(self):
        class Context:
            calls=[]
            def register_tool(self,**kwargs): self.calls.append(kwargs)
        ctx=Context(); plugin.register(ctx)
        self.assertEqual(len(ctx.calls),1); self.assertEqual(ctx.calls[0]["name"],"host_android")
        handler=ctx.calls[0]["handler"]
        self.assertFalse(decode(handler({"action":"devices","params":{},"extra":True}))["ok"])
        self.assertFalse(decode(handler(None))["ok"])

    def test_device_package_and_confirm_validation(self):
        for serial in ("-s", "emulator-1;id", "device", "emulator-1\n"):
            self.assertFalse(decode(plugin.host_android("key", {"device": serial, "key": "back"}))["ok"])
        self.assertFalse(decode(plugin.host_android("launch", {"device":"emulator-1","package":"-p","confirm":True}))["ok"])
        self.assertFalse(decode(plugin.host_android("launch", {"device":"emulator-1","package":"com.shi0203.MyJune","confirm":False}))["ok"])

    def test_remote_shell_has_only_finite_literals(self):
        with patch.object(plugin,"_adb") as adb:
            plugin._action_key({"device":"emulator-5554","key":"back","confirm":True})
            self.assertEqual(adb.call_args.args[1], ["shell","input","keyevent","KEYCODE_BACK"])
        for text in ("-x", "hello;id", "hello$HOME", "'quoted'", "hello\nworld", "日本語", "a"*257):
            with self.assertRaises(plugin.AndroidError): plugin._action_type({"device":"emulator-5554","text":text,"workdir":"coding"})

    def test_orca_context_is_explicit_and_allowlisted(self):
        with self.assertRaises(plugin.AndroidError): plugin._workdir("/tmp")
        with self.assertRaises(plugin.AndroidError): plugin._workdir("../coding")
        with patch.object(plugin,"_orca",return_value={"ok":True}) as call:
            plugin._action_ax({"device":"emulator-5554","workdir":"coding"})
            self.assertIn("--worktree",call.call_args.args[2])

    def test_absolute_worktree_is_validated_and_avd_attach_selector_is_narrow(self):
        with tempfile.TemporaryDirectory() as td:
            candidate=Path(td)/"repo"; candidate.mkdir()
            with patch.dict(plugin.REPOS,{"myjune-worktrees":Path(td)}):
                self.assertEqual(plugin._workdir(str(candidate)),candidate)
                with self.assertRaises(plugin.AndroidError): plugin._workdir("/tmp")
        self.assertEqual(plugin._attach_device("MyJune_API_36"),"MyJune_API_36")
        with self.assertRaises(plugin.AndroidError): plugin._device("MyJune_API_36")

    def test_path_traversal_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); outside=root.parent/(root.name+"-outside.apk"); outside.write_bytes(b"x")
            link=root/"escape.apk"; link.symlink_to(outside)
            with self.assertRaises(plugin.AndroidError): plugin._safe_path(str(link),suffix=".apk",roots=(root,))
            with self.assertRaises(plugin.AndroidError): plugin._reject_symlink_components(link)
            outside.unlink()

    def test_argv_and_environment_are_fixed(self):
        with patch.dict(os.environ,{"AWS_SECRET_ACCESS_KEY":"not-forwarded","ORCA_ENVIRONMENT":"local-test"},clear=False):
            env=plugin._clean_env(orca=True)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY",env)
        self.assertEqual(env["ORCA_ENVIRONMENT"],"local-test")
        self.assertNotIn("TERMINAL_DOCKER_VOLUMES",env)

    def test_orca_error_envelope_and_failed_exit(self):
        with patch.object(plugin,"_run",return_value=(0,'{"ok":false,"error":"x"}',"")):
            with self.assertRaises(plugin.AndroidError): plugin._orca("ax",Path("/tmp"),["emulator","ax"])
        with self.assertRaises(plugin.AndroidError): plugin._require_ok(7,"","token=secret","adb")

    def test_bounded_output_and_redaction(self):
        code,out,err=plugin._run(["/usr/bin/python3","-c","import sys;print('x'*10000);print('token=secret',file=sys.stderr)"],env=plugin._clean_env(),max_bytes=200)
        self.assertEqual(code,0); self.assertLessEqual(len(out),250); self.assertIn("[output truncated]",out); self.assertIn("[redacted]",err); self.assertNotIn("secret",err)
        for secret in ("ghp_"+"a"*30,"sk-"+"b"*30): self.assertNotIn(secret,plugin._redact(secret))

    def test_binary_output_overflow_fails(self):
        with self.assertRaises(plugin.AndroidError):
            plugin._run(["/usr/bin/python3","-c","import sys;sys.stdout.buffer.write(b'x'*4096)"],env=plugin._clean_env(),binary=True,max_bytes=128)

    def test_timeout_kills_descendant_holding_pipe(self):
        import time
        code="import subprocess,time; subprocess.Popen(['/usr/bin/python3','-c','import time;time.sleep(10)']); time.sleep(10)"
        started=time.monotonic()
        with self.assertRaises(plugin.AndroidError): plugin._run(["/usr/bin/python3","-c",code],env=plugin._clean_env(),timeout=1)
        self.assertLess(time.monotonic()-started,4)

    def test_devices_reports_adb_and_orca_independently(self):
        with patch.object(plugin,"_run",return_value=(1,"","socket unavailable")),patch.object(plugin,"_orca",side_effect=plugin.AndroidError("runtime unavailable")):
            result=plugin._action_devices({})
        self.assertEqual(result["adb"],[]); self.assertIn("socket unavailable",result["adb_error"]); self.assertIn("runtime unavailable",result["orca_error"])


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.old_evidence=plugin.EVIDENCE; plugin.EVIDENCE=self.root/"evidence"
        self.addCleanup(setattr,plugin,"EVIDENCE",self.old_evidence)

    def write_job(self, **changes):
        job={"id":"12345678-1234-4234-8234-123456789abc","device":"emulator-5554","duration":3,"status":"complete","created":1,"output":str(plugin.EVIDENCE/"12345678-1234-4234-8234-123456789abc"/"recording.mp4"),"remote":"/sdcard/host_android_12345678-1234-4234-8234-123456789abc.mp4","pid":99999999,"proc_start":"1"}
        job.update(changes); plugin._atomic_json(plugin._job_path(job["id"]),job); return job

    def test_status_and_stale_worker_detection(self):
        job=self.write_job(status="running")
        result=decode(plugin.host_android("record_status",{"job_id":job["id"]}))
        self.assertEqual(result["result"]["status"],"failed")

    def test_collect_refuses_running_and_pull_is_after_completion(self):
        job=self.write_job(status="running")
        with self.assertRaises(plugin.AndroidError): plugin._action_record_collect({"job_id":job["id"]})
        job["status"]="complete"; plugin._atomic_json(plugin._job_path(job["id"]),job)
        order=[]
        def run(argv,**kwargs):
            order.append(argv[0]);
            if "pull" in argv: Path(argv[-1]).write_bytes(b"fake-mp4")
            return 0,"ok",""
        with patch.object(plugin,"_run",side_effect=run),patch.object(plugin,"_valid_recording",side_effect=lambda p:(3.0,"h264")):
            result=plugin._action_record_collect({"job_id":job["id"]})
        self.assertEqual(order[0],str(plugin.ADB)); self.assertEqual(result["video_codec"],"h264")

    def test_collect_is_idempotent_after_verified_file_exists(self):
        job=self.write_job(status="collected",duration_verified=3.0,codec="h264")
        target=Path(job["output"]); target.parent.mkdir(parents=True); target.write_bytes(b"fixture")
        with patch.object(plugin,"_valid_recording",return_value=(3.0,"h264")),patch.object(plugin,"_run") as run:
            result=plugin._action_record_collect({"job_id":job["id"]})
        run.assert_not_called(); self.assertTrue(result["idempotent"]); self.assertTrue(target.exists())

    def test_job_json_symlink_and_job_directory_symlink_are_rejected(self):
        job=self.write_job(); path=plugin._job_path(job["id"]); real=path.with_suffix(".real")
        path.rename(real); path.symlink_to(real)
        with self.assertRaises(plugin.AndroidError): plugin._read_job(job["id"])
        path.unlink(); real.unlink()
        jobs=plugin.EVIDENCE/".host-android-jobs"; jobs.rmdir(); jobs.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(plugin.AndroidError): plugin._jobs_dir()

    def test_collect_failure_never_leaves_final_media(self):
        job=self.write_job(); target=Path(job["output"]); target.parent.mkdir(parents=True)
        def failed_pull(argv,**kwargs):
            Path(argv[-1]).write_bytes(b"partial"); raise plugin.AndroidError("uncertain transfer")
        with patch.object(plugin,"_run",side_effect=failed_pull):
            with self.assertRaises(plugin.AndroidError): plugin._action_record_collect({"job_id":job["id"]})
        self.assertFalse(target.exists()); self.assertFalse((target.parent/f".recording-{job['id']}.mp4.part").exists())

    def test_mp4_validation_failure_deletes_partial_file(self):
        job=self.write_job(); target=Path(job["output"]); target.parent.mkdir(parents=True); target.write_bytes(b"bad")
        with patch.object(plugin,"_run",return_value=(0,"{}","")):
            with self.assertRaises(plugin.AndroidError): plugin._valid_recording(target)

    def test_record_start_requires_confirm_and_duration(self):
        with self.assertRaises(plugin.AndroidError): plugin._action_record_start({"device":"emulator-5554","duration":3,"confirm":False})
        with self.assertRaises(plugin.AndroidError): plugin._action_record_start({"device":"emulator-5554","duration":181,"confirm":True})

    def test_per_device_recording_conflict(self):
        job=self.write_job(status="running",pid=os.getpid(),proc_start=plugin._proc_identity(os.getpid()))
        lock=plugin._jobs_dir()/"device-emulator-5554.lock"; lock.write_text(job["id"])
        with self.assertRaises(plugin.AndroidError): plugin._action_record_start({"device":"emulator-5554","duration":3,"confirm":True})

    def test_worker_fixed_argv_and_natural_completion(self):
        job=self.write_job(status="running",pid=os.getpid())
        with patch.object(plugin,"_run",return_value=(0,"","")) as run:
            code=plugin._record_worker(job["id"])
        self.assertEqual(code,0)
        argv=run.call_args.args[0]
        self.assertEqual(argv,[str(plugin.ADB),"-s","emulator-5554","shell","screenrecord","--time-limit","3","/sdcard/host_android_12345678-1234-4234-8234-123456789abc.mp4"])
        self.assertEqual(plugin._read_job(job["id"])["status"],"complete")

    def test_synthetic_ffmpeg_mp4_passes_real_verifier(self):
        if not plugin.FFMPEG.is_file() or not plugin.FFPROBE.is_file(): self.skipTest("configured ffmpeg tools unavailable")
        path=self.root/"synthetic-validation-only.mp4"
        plugin._run([str(plugin.FFMPEG),"-v","error","-f","lavfi","-i","color=c=black:s=64x64:d=0.4","-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart","-y",str(path)],env=plugin._clean_env(),timeout=30)
        duration,codec=plugin._valid_recording(path)
        self.assertGreater(duration,0); self.assertEqual(codec,"h264")

    def test_start_status_collect_lifecycle_across_processes(self):
        job_id="12345678-1234-4234-8234-123456789abc"
        module=str(PLUGIN)
        worker_code=f'''import importlib.util,os
import sys
p=importlib.util.spec_from_file_location("worker_plugin",{module!r}); m=importlib.util.module_from_spec(p); p.loader.exec_module(m)
from pathlib import Path
m.EVIDENCE=Path({str(self.root / "evidence")!r})
m._run=lambda *a,**k:(0,"","")
raise SystemExit(m._record_worker(sys.argv[1]))'''
        spawned=[]; real_popen=subprocess.Popen
        def capture_popen(*args,**kwargs):
            kwargs["stdout"]=subprocess.PIPE; kwargs["stderr"]=subprocess.PIPE
            proc=real_popen(*args,**kwargs); spawned.append(proc); return proc
        with patch.object(plugin,"_worker_command",side_effect=lambda actual_id: [__import__("sys").executable,"-c",worker_code,actual_id]),patch.object(plugin.subprocess,"Popen",side_effect=capture_popen):
            started=plugin._action_record_start({"device":"emulator-5554","duration":1,"confirm":True})
        job_id=started["job_id"]
        import time
        deadline=time.monotonic()+5
        while plugin._action_record_status({"job_id":job_id})["status"] not in {"complete","failed"} and time.monotonic()<deadline: time.sleep(.02)
        child_output=spawned[0].communicate(timeout=3)
        self.assertEqual(spawned[0].returncode,0,child_output)
        env=plugin._clean_env()
        self.assertEqual(plugin._action_record_status({"job_id":job_id})["status"],"complete")
        collect_code=f'''import importlib.util,os
p=importlib.util.spec_from_file_location("collect_plugin",{module!r}); m=importlib.util.module_from_spec(p); p.loader.exec_module(m)
from pathlib import Path
m.EVIDENCE=Path({str(self.root / "evidence")!r})
def run(argv,**kwargs): Path(argv[-1]).write_bytes(b"verified fixture"); return (0,"ok","")
m._run=run; m._valid_recording=lambda path:(3.0,"h264")
m._action_record_collect({{"job_id":{job_id!r}}})'''
        subprocess.run([__import__("sys").executable,"-c",collect_code],env=env,check=True,timeout=10)
        self.assertEqual(plugin._read_job(job_id)["status"],"collected")
        self.assertTrue(Path(plugin._read_job(job_id)["output"]).is_file())


if __name__ == "__main__": unittest.main()
