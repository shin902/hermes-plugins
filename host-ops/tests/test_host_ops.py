import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import __init__ as plugin


class HostOpsTests(unittest.TestCase):
    def test_docker_inspect_public_handler_accepts_profile(self):
        with mock.patch.object(plugin, "_docker_inspect", return_value=[]) as inspect:
            result = json.loads(plugin.handle({"action": "docker_inspect", "params": {"profile": "default"}}))
        self.assertTrue(result["ok"])
        inspect.assert_called_once_with("default")

    def test_schema_registration_public_api(self):
        class Ctx:
            def register_tool(self, **kw): self.kw=kw
        ctx=Ctx(); plugin.register(ctx)
        self.assertEqual(ctx.kw["name"],"host_ops")
        self.assertIs(ctx.kw["handler"],plugin.handle)

    def test_strict_validation(self):
        for payload in [
            {"action":"health_check","params":{"endpoint":"http://evil"}},
            {"action":"service_restart","params":{"unit":"x.service","confirm":True}},
            {"action":"config_set","params":{"key":"terminal.timeout","value":True}},
            {"action":"cron_run","params":{"job_id":"../foo","confirm":True}},
            {"action":"docker_inspect","params":{"profile":"../../coding"}},
            {"action":"host_diagnostics","params":{"anything":"raw"}},
        ]:
            with self.subTest(payload=payload):
                out=json.loads(plugin.handle(payload)); self.assertFalse(out["ok"])

    def test_config_types_and_confirm(self):
        plugin._validate("config_set",{"key":"terminal.timeout","value":600,"confirm":True})
        with self.assertRaises(ValueError): plugin._validate("config_set",{"key":"terminal.timeout","value":0,"confirm":True})
        with self.assertRaises(ValueError): plugin._validate("config_set",{"key":"terminal.timeout","value":2})
        with self.assertRaises(ValueError): plugin._validate("service_stop",{"unit":"llama-swap.service","confirm":"true"})
        with self.assertRaises(ValueError): plugin._validate("cron_run",{"job_id":"abcdef12","confirm":False})

    def test_http_redirect_denied_and_response_limited(self):
        with mock.patch.object(plugin,"build_opener") as opener:
            response=mock.MagicMock(); response.status=200; response.read.return_value=b"x"*4097
            opener.return_value.open.return_value.__enter__.return_value=response
            result=plugin._health("preview")
            self.assertTrue(result["ok"]); self.assertTrue(result["truncated"]); self.assertEqual(len(result["body"]),4096)
            req=opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url,plugin.ENDPOINTS["preview"]); self.assertEqual(req.get_method(),"GET")
        with mock.patch.object(plugin,"build_opener") as opener:
            opener.return_value.open.side_effect=ValueError("redirect denied")
            self.assertIn("redirect denied",plugin._health("metro")["error"])

    def test_stream_capture_bounds(self):
        with self.assertRaisesRegex(RuntimeError,"exceeded limit"):
            plugin._run([sys.executable,"-c","import sys;sys.stdout.write('x'*30000)"],timeout=3,max_bytes=1024)

    def test_timeout_kills_process_group_with_retained_pipe(self):
        code="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)"
        started=time.monotonic()
        with self.assertRaises(TimeoutError): plugin._run([sys.executable,"-c",code],timeout=.2,max_bytes=2048)
        self.assertLess(time.monotonic()-started,4)

    def test_parent_exit_with_pipe_holding_descendant_is_bounded(self):
        code="import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
        started=time.monotonic()
        with self.assertRaises(TimeoutError): plugin._run([sys.executable,"-c",code],timeout=2,max_bytes=2048)
        self.assertLess(time.monotonic()-started,3)

    def test_malformed_unhashable_inputs_are_json_errors(self):
        for payload in [
            {"action": [], "params": {}},
            {"action": "service_status", "params": {"unit": []}},
            {"action": "health_check", "params": {"endpoint": []}},
            {"action": "host_diagnostics", "params": {"x": []}},
        ]:
            out=json.loads(plugin.handle(payload)); self.assertFalse(out["ok"])

    def test_bearer_and_github_credentials_redacted_recursively(self):
        value={"nested":["Bearer abcdefghijklmnop", "gho_"+"a"*30, "github_pat_"+"b"*30]}
        redacted=plugin._redact_tree(value)
        self.assertNotIn("abcdefghijklmnop",json.dumps(redacted))
        self.assertNotIn("gho_",json.dumps(redacted)); self.assertNotIn("github_pat_",json.dumps(redacted))

    def test_cron_profile_is_action_scoped(self):
        self.assertEqual(plugin._validate("cron_status",{"profile":"coding"}),{"profile":"coding"})
        with self.assertRaises(ValueError): plugin._validate("config_get",{"key":"terminal.backend","profile":"default"})

    @mock.patch.object(plugin,"_hermes",return_value=(4,"","failed"))
    def test_cron_read_only_nonzero_exit_is_not_ok(self, hermes):
        result=json.loads(plugin.handle({"action":"cron_status","params":{}}))
        self.assertFalse(result["ok"]); self.assertFalse(result["result"]["ok"])

    @mock.patch.object(plugin,"_run",return_value=(4,"","failed"))
    def test_service_logs_nonzero_exit_is_not_ok(self, run):
        result=json.loads(plugin.handle({"action":"service_logs","params":{"unit":"hermes-gateway.service"}}))
        self.assertFalse(result["ok"]); self.assertFalse(result["result"]["ok"])

    @mock.patch.object(plugin,"_run",return_value=(0,"",""))
    def test_daemon_reload_is_reported_executed_but_not_verified(self, run):
        result=plugin._service_mutation("daemon_reload",{})
        self.assertTrue(result["ok"]); self.assertFalse(result["verified"])

    @mock.patch.object(plugin,"_run")
    def test_service_readback_controls_verification(self, run):
        run.side_effect=[(0,"",""),(0,"active\n","")]
        result=plugin._service_mutation("service_restart",{"unit":"hermes-gateway.service"})
        self.assertTrue(result["verified"]); self.assertTrue(result["readback"]["active"])
        run.side_effect=[(1,"","failed"),(3,"inactive\n","")]
        result=plugin._service_mutation("service_start",{"unit":"hermes-gateway.service"})
        self.assertFalse(result["verified"]); self.assertFalse(result["retry_safe"])

    @mock.patch.object(plugin,"_cron_job_readback",return_value={"id":"abcdef12","enabled":True,"next_run_at":"2026-10-13T00:00:00+00:00"})
    @mock.patch.object(plugin,"_hermes")
    def test_cron_run_uses_installed_cli_completion_semantics(self, hermes, readback):
        hermes.return_value=(0,"Triggered job\nRan now: succeeded.", "")
        result=json.loads(plugin.handle({"action":"cron_run","params":{"job_id":"abcdef12","confirm":True}}))
        self.assertTrue(result["ok"]); data=result["result"]
        self.assertFalse(data["queued"]); self.assertTrue(data["completed"]); self.assertFalse(data["retry_safe"])

    @mock.patch.object(plugin,"_hermes")
    def test_config_set_exact_typed_readback(self, hermes):
        hermes.side_effect=[(0,"saved", ""),(0,"true\n","")]
        result=json.loads(plugin.handle({"action":"config_set","params":{"key":"model.supports_vision","value":True,"confirm":True}}))
        self.assertTrue(result["result"]["verified"])
        hermes.side_effect=[(0,"saved", ""),(0,"1\n","")]
        result=json.loads(plugin.handle({"action":"config_set","params":{"key":"model.supports_vision","value":True,"confirm":True}}))
        self.assertFalse(result["result"]["verified"])

    @mock.patch.object(plugin,"_docker_list",return_value=[{"id":"id","name":"hermes-coding","image":"image","status":"Up"}])
    @mock.patch.object(plugin,"_run",return_value=(0,'{"hermes-agent":"1","hermes-profile":"coding"} []',''))
    def test_docker_inspect_label_scope(self, run, listing):
        self.assertEqual(plugin._docker_inspect("coding"),[])
        self.assertEqual(run.call_count,1)

    def test_env_is_deterministic_and_no_shell(self):
        with mock.patch.dict(os.environ,{"LD_PRELOAD":"bad.so","PYTHONPATH":"/tmp/bad","TOKEN":"secret"}):
            env=plugin._env("coding")
        self.assertNotIn("LD_PRELOAD",env); self.assertNotIn("PYTHONPATH",env); self.assertNotIn("TOKEN",env)
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],"unix:path=/run/user/1000/bus")


if __name__ == "__main__": unittest.main()
