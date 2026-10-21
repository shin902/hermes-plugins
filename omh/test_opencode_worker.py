"""Runnable tests for the profile-local OpenCode implementation worker.

Run from the repository root:

    python3 -m unittest discover -s omh -p 'test_opencode_worker.py' -v

The tests use small local fake executables, so they do not contact OpenCode or
any provider.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest


_MODULE_PATH = Path(__file__).with_name("opencode_worker.py")
_SPEC = importlib.util.spec_from_file_location("omh_opencode_worker_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


class OpenCodeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()

    def fake_script(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_argv_pins_model_and_standalone(self):
        argv = worker.build_argv(
            opencode_bin=Path("/opt/opencode"), prompt="do work", session_id="ses_abc123"
        )
        self.assertEqual(
            argv,
            [
                "/opt/opencode",
                "run",
                "--standalone",
                "--model",
                worker.WORKER_MODEL,
                "--format",
                "json",
                "--session",
                "ses_abc123",
                "--",
                "do work",
            ],
        )
        self.assertNotIn("--auto", argv)
        self.assertIn("--auto", worker.build_argv(
            opencode_bin=Path("/opt/opencode"), prompt="do work", auto=True
        ))

    def test_runs_in_explicit_cwd_with_pinned_model(self):
        fake = self.fake_script(
            "fake_opencode.py",
            """
            import json
            import os
            import sys

            argv = sys.argv[1:]
            model = argv[argv.index("--model") + 1]
            session = argv[argv.index("--session") + 1] if "--session" in argv else "ses_fake0001"
            print(json.dumps({"type": "step_start", "sessionID": session,
                              "part": {"type": "step-start"}}))
            print(json.dumps({"type": "text", "sessionID": session,
                              "part": {"type": "text", "text": json.dumps({
                                  "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
                                  "model": model, "auto": "--auto" in argv})}}))
            """,
        )

        report = worker.run_worker(cwd=self.cwd, prompt="do work", opencode_bin=fake)

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["session_id"], "ses_fake0001")
        self.assertFalse(report["resumed"])
        observed = json.loads(report["text"])
        self.assertEqual(observed["cwd"], str(self.cwd.resolve()))
        self.assertEqual(observed["pwd"], str(self.cwd.resolve()))
        self.assertEqual(observed["model"], worker.WORKER_MODEL)
        self.assertFalse(observed["auto"])

    def test_resumes_requested_session_and_reads_it_back(self):
        fake = self.fake_script(
            "fake_resume.py",
            """
            import json
            import sys

            argv = sys.argv[1:]
            session = argv[argv.index("--session") + 1]
            print(json.dumps({"type": "text", "sessionID": session,
                              "part": {"type": "text", "text": "RESUMED"}}))
            """,
        )

        report = worker.run_worker(
            cwd=self.cwd,
            prompt="continue",
            session_id="ses_resume123",
            opencode_bin=fake,
        )

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["session_id"], "ses_resume123")
        self.assertEqual(report["requested_session_id"], "ses_resume123")
        self.assertTrue(report["resumed"])
        self.assertEqual(report["text"], "RESUMED")

    def test_bounded_timeout_is_reported(self):
        fake = self.fake_script("fake_slow.py", "import time\ntime.sleep(10)\n")

        report = worker.run_worker(
            cwd=self.cwd, prompt="slow", timeout=1, opencode_bin=fake
        )

        self.assertFalse(report["ok"])
        self.assertTrue(report["timed_out"])
        self.assertEqual(report["exit_code"], 124)
        self.assertIsNone(report["opencode_exit_code"])
        self.assertLess(report["duration_seconds"], 8)

    def test_nonzero_exit_is_not_reported_as_success(self):
        fake = self.fake_script(
            "fake_fail.py",
            """
            import json
            import sys

            print(json.dumps({"type": "step_start", "sessionID": "ses_fail0001",
                              "part": {"type": "step-start"}}))
            sys.stderr.write("provider failed\\n")
            sys.exit(7)
            """,
        )

        report = worker.run_worker(cwd=self.cwd, prompt="fail", opencode_bin=fake)

        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 7)
        self.assertEqual(report["opencode_exit_code"], 7)
        self.assertEqual(report["session_id"], "ses_fail0001")
        self.assertIn("exited with code 7", report["error"])
        self.assertIn("provider failed", report["stderr"])

    def test_session_mismatch_is_not_silently_treated_as_resume(self):
        fake = self.fake_script(
            "fake_mismatch.py",
            """
            import json

            print(json.dumps({"type": "text", "sessionID": "ses_other999",
                              "part": {"type": "text", "text": "WRONG"}}))
            """,
        )

        report = worker.run_worker(
            cwd=self.cwd,
            prompt="resume",
            session_id="ses_wanted123",
            opencode_bin=fake,
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("instead of requested", report["error"])

    def test_error_event_with_zero_exit_is_a_failure(self):
        fake = self.fake_script(
            "fake_error_event.py",
            """
            import json

            print(json.dumps({"type": "step_start", "sessionID": "ses_error0001",
                              "part": {"type": "step-start"}}))
            print(json.dumps({"type": "error", "sessionID": "ses_error0001",
                              "error": {"name": "ProviderAuthError"}}))
            """,
        )

        report = worker.run_worker(cwd=self.cwd, prompt="work", opencode_bin=fake)

        self.assertFalse(report["ok"], report)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["opencode_exit_code"], 0)
        self.assertEqual(report["session_id"], "ses_error0001")
        self.assertIn("error event", report["error"])

    def test_invalid_json_output_is_a_protocol_failure(self):
        fake = self.fake_script("fake_not_json.py", 'print("not json")\n')

        report = worker.run_worker(cwd=self.cwd, prompt="work", opencode_bin=fake)

        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("JSON output was invalid", report["error"])

    def test_validation_refuses_unsafe_arguments(self):
        with self.assertRaises(worker.WorkerError):
            worker.resolve_cwd("relative/path")
        with self.assertRaises(worker.WorkerError):
            worker.resolve_cwd(str(self.root / "missing"))
        with self.assertRaises(worker.WorkerError):
            worker.resolve_opencode_bin(self.root / "missing")
        with self.assertRaises(worker.WorkerError):
            worker.validate_session_id("ses-bad")
        with self.assertRaises(worker.WorkerError):
            worker.validate_session_id("--help")
        with self.assertRaises(worker.WorkerError):
            worker.validate_timeout(0)
        with self.assertRaises(worker.WorkerError):
            worker.validate_timeout(worker.MAX_TIMEOUT_SECONDS + 1)
        with self.assertRaises(worker.WorkerError):
            worker.validate_prompt("  ")
        with self.assertRaises(worker.WorkerError):
            worker.validate_prompt("x" * (worker.MAX_PROMPT_CHARS + 1))
        with self.assertRaises(worker.WorkerError):
            worker.validate_prompt("bad\x00prompt")

    def test_main_prints_json_and_propagates_failure_code(self):
        fake = self.fake_script("fake_cli_fail.py", "import sys\nsys.exit(9)\n")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = worker.main(
                ["--cwd", str(self.cwd), "--prompt", "work", "--opencode-bin", str(fake)]
            )

        self.assertEqual(code, 9)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 9)
        self.assertEqual(report["model"], worker.WORKER_MODEL)


if __name__ == "__main__":
    unittest.main()
