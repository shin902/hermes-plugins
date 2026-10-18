import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("host_github", Path(__file__).parents[1] / "__init__.py")
hg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hg)


def args(action, params=None):
    return {"action": action, "repo": "owner/repo", "params": params or {}}


class ValidationTests(unittest.TestCase):
    def test_unknown_action_and_parameters_rejected(self):
        for action, repo, params in (("api", "o/r", {}), ("repo_view", "o/r", {"endpoint": "/user"})):
            with self.assertRaises(ValueError): hg.validate(action, repo, params)

    def test_repo_and_option_injection_rejected(self):
        for repo in ("-R/o", "owner/repo --help", "owner/../repo", "owner/repo\n"):
            with self.assertRaises(ValueError): hg.validate("repo_view", repo, {})
        for action, params in (("pr_view", {"number": "--help"}),
                               ("pr_create", {"head": "--repo=attacker/x", "base": "main", "title": "x", "confirm": True})):
            with self.assertRaises(ValueError): hg.validate(action, "owner/repo", params)

    def test_allowlist(self):
        with patch.dict("os.environ", {"HOST_GITHUB_ALLOWED_REPOS": "owner/allowed"}):
            with self.assertRaises(ValueError): hg.validate("repo_view", "owner/other", {})
            self.assertEqual(hg.validate("repo_view", "owner/allowed", {}), {})

    def test_unhashable_enum_values_rejected_cleanly(self):
        for key in ("state", "status", "event"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                hg.validate("run_list", "owner/repo", {key: []})
        with self.assertRaises(ValueError): hg.validate([], "owner/repo", {})

    def test_run_list_accepts_gh_supported_statuses(self):
        for value in ("success", "failure", "startup_failure", "timed_out"):
            self.assertEqual(hg.validate("run_list", "owner/repo", {"status": value})["status"], value)

    def test_label_and_list_validation(self):
        for p in ({"label": []}, {"labels": [None]}, {"labels": ["x"] * 21}):
            with self.subTest(p=p), self.assertRaises(ValueError): hg.validate("issue_list", "owner/repo", p)
        with self.assertRaises(ValueError): hg.validate("issue_create", "owner/repo", {"title": "t", "body": "b", "confirm": True, "labels": [3]})

    def test_comment_actions_removed_and_schema_maps_action_parameters(self):
        for action in ("pr_comment", "issue_comment"):
            self.assertNotIn(action, hg.ACTIONS)
        mapping = hg.SCHEMA["parameters"]["properties"]["params"]["x-action-parameters"]
        self.assertEqual(set(mapping), hg.ACTIONS)
        self.assertEqual(mapping["pr_create"]["allowed"], sorted({"title", "body", "head", "base", "draft", "confirm"}))
        self.assertEqual(mapping["pr_create"]["required"], ["title", "head", "base", "confirm"])

    def test_write_requirements_and_top_level_validation(self):
        with patch.object(hg, "_run") as run:
            result = json.loads(hg.handle({**args("issue_create", {"title": "t", "body": "b"}), "extra": True}))
        self.assertFalse(result["ok"])
        run.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def test_fixed_argv_and_minimal_environment(self):
        with patch.dict("os.environ", {"GH_DEBUG": "api", "GH_HOST": "attacker", "GITHUB_TOKEN": "secret"}, clear=True):
            with patch.object(hg, "_run", return_value=(0, '{"url":"ok"}', "")) as run:
                self.assertTrue(json.loads(hg.handle(args("repo_view")))["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], [hg.GH, "repo", "view"])
        self.assertEqual(argv[3], "owner/repo")

    def test_local_subprocess_output_bounds_on_both_streams(self):
        script = "import sys; sys.stdout.write('x' * 2000000); sys.stderr.write('y' * 2000000)"
        with patch.object(hg, "GH", sys.executable), patch.object(hg, "MAX_OUTPUT", 4096):
            with self.assertRaisesRegex(RuntimeError, "output exceeded limit"):
                hg._run([sys.executable, "-c", script])

    def test_local_subprocess_timeout_is_terminated_and_reaped(self):
        script = "import time; print('ready', flush=True); time.sleep(30)"
        with patch.object(hg, "GH", sys.executable), patch.object(hg, "TIMEOUT", 0.1):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                hg._run([sys.executable, "-c", script])

    def test_local_subprocess_successful_text_is_redacted_without_json_truncation(self):
        secret = "ghp_" + "a" * 30
        value = {"text": "x" * 3000, "token": secret}
        code = "import json; print(json.dumps(" + repr(value) + "))"
        with patch.object(hg, "GH", sys.executable):
            rc, out, _ = hg._run([sys.executable, "-c", code])
        self.assertEqual(rc, 0)
        result = json.loads(json.dumps({"result": hg._redact(out.strip())}))
        self.assertEqual(len(json.loads(result["result"])["text"]), 3000)
        self.assertNotIn(secret, result["result"])

    def test_write_exact_readback_verified(self):
        expected = {"number": 4, "title": "t", "body": "body", "headRefName": "feature", "baseRefName": "main"}
        with patch.object(hg, "_run", side_effect=[(0, "https://github.com/owner/repo/pull/4\n", ""), (0, json.dumps(expected), "")]):
            result = json.loads(hg.handle(args("pr_create", {"title": "t", "body": "body", "head": "feature", "base": "main", "confirm": True})))
        payload = json.loads(result["result"])
        self.assertEqual((payload["write_succeeded"], payload["verified"], payload["retry_safe"]), (True, True, False))

    def test_write_mismatch_and_readback_failure_are_not_retryable(self):
        cases = [
            [(0, "https://github.com/owner/repo/issues/7\n", ""), (0, '{"title":"else","body":"b"}', "")],
            [(0, "https://github.com/owner/repo/issues/7\n", ""), (1, "", "denied")],
        ]
        for side_effect in cases:
            with patch.object(hg, "_run", side_effect=side_effect):
                result = json.loads(hg.handle(args("issue_create", {"title": "t", "body": "b", "confirm": True})))
            self.assertEqual((result["write_succeeded"], result["verified"], result["retry_safe"]), (True, False, False))

    def test_write_timeout_or_unknown_has_machine_readable_nonretryable_outcome(self):
        with patch.object(hg, "_run", side_effect=RuntimeError("gh timed out")):
            result = json.loads(hg.handle(args("issue_create", {"title": "t", "body": "b", "confirm": True})))
        self.assertIsNone(result["write_succeeded"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["retry_safe"])

    def test_created_write_url_must_identify_correct_target(self):
        with patch.object(hg, "_run", return_value=(0, "https://github.com/other/repo/issues/7", "")):
            result = json.loads(hg.handle(args("issue_create", {"title": "t", "body": "b", "confirm": True})))
        self.assertEqual((result["write_succeeded"], result["verified"], result["retry_safe"]), (True, False, False))

    def test_audit_uses_validated_payload_only(self):
        with patch.object(hg, "_audit") as audit:
            hg.handle({"action": [], "repo": {"malicious": True}, "params": []})
        self.assertEqual(audit.call_args.args[:2], ("unknown", "unknown"))


if __name__ == "__main__": unittest.main()
