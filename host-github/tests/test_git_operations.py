"""Exercise host Git against disposable local repositories, never GitHub writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("git_test_plugin", Path(__file__).parents[1] / "host_github.py")
assert spec is not None and spec.loader is not None
hg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hg)


class GitTests(unittest.TestCase):
    def test_validation_and_no_approval_registration(self):
        for action, params in [
            ("git_commit", {"cwd": "/x"}),
            ("git_status", {"cwd": "relative"}),
            ("git_add", {"cwd": "/x", "paths": ["../outside"]}),
            ("git_add", {"cwd": "/x", "paths": []}),
            ("git_fetch", {"cwd": "/x", "branch": "--upload-pack=bad"}),
            ("git_push", {"cwd": "/x", "branch": "x:main"}),
            ("git_push", {"cwd": "/x", "branch": "main", "force": True}),
            ("git_diff", {"cwd": "/x", "staged": "yes"}),
        ]:
            with self.subTest(action=action, params=params), self.assertRaises(ValueError):
                hg.validate(action, "owner/repo", params)
        class Context:
            def register_tool(self, **kw): self.tool = kw
        ctx = Context()
        hg.register(ctx)  # No approval hook or middleware is registered.
        self.assertEqual(ctx.tool["name"], "host_github")
        self.assertTrue(hg.GIT_ACTIONS <= set(ctx.tool["schema"]["parameters"]["properties"]["action"]["enum"]))

    def test_real_git_lifecycle_and_worktree(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            root = Path(tmp)
            checkout, remote = root / "checkout", root / "remote.git"
            checkout.mkdir()
            def git(*args, cwd=checkout):
                return subprocess.check_output([hg.GIT, *args], cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
            git("init", "--initial-branch=main")
            git("init", "--bare", str(remote))
            git("config", "user.name", "Fixture User")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            git("config", "core.hooksPath", "/dev/null")
            # Intentional normal Git URL rewrite, confined to this fixture repository.
            git("config", f"url.{remote}.insteadOf", "https://github.com/owner/repo.git")
            def call(action, **params):
                payload = {"action": action, "repo": "owner/repo", "params": {"cwd": str(checkout), **params}}
                with patch.object(hg, "_audit"):
                    result = json.loads(hg.handle(payload))
                self.assertTrue(result["ok"], result)
                return result["result"]
            (checkout / "example.txt").write_text("first\n")
            self.assertIn("example.txt", call("git_status")["output"])
            call("git_add", paths=["example.txt"])
            self.assertIn("+first", call("git_diff", staged=True)["output"])
            first = call("git_commit", message="fixture commit")["commit"]
            self.assertEqual(first, git("rev-parse", "HEAD"))
            self.assertIn("fixture commit", call("git_log", limit=1)["output"])
            self.assertTrue(call("git_push", branch="main")["verified"])
            self.assertEqual(first, git("--git-dir=" + str(remote), "rev-parse", "refs/heads/main"))
            self.assertEqual(call("git_fetch", branch="main")["commit"], first)
            (checkout / "example.txt").write_text("second\n")
            call("git_add", paths=["example.txt"])
            second = call("git_commit", message="second fixture commit")["commit"]
            call("git_push", branch="main")
            git("reset", "--hard", first)  # Only this disposable checkout is reset.
            with patch.object(hg, "_audit"):
                rejected = json.loads(hg.handle({"action": "git_push", "repo": "owner/repo", "params": {"cwd": str(checkout), "branch": "main"}}))
            self.assertFalse(rejected["ok"], rejected)
            self.assertFalse(rejected["result"]["retry_safe"])
            self.assertEqual(second, git("--git-dir=" + str(remote), "rev-parse", "refs/heads/main"))
            worktree = root / "worktree"
            git("worktree", "add", "-b", "feature", str(worktree))
            with patch.object(hg, "_audit"):
                result = json.loads(hg.handle({"action": "git_status", "repo": "owner/repo", "params": {"cwd": str(worktree)}}))
            self.assertTrue(result["ok"], result)
            self.assertIn("feature", result["result"]["output"])

    def test_push_timeout_and_readback_failure_are_not_retryable(self):
        sha = "a" * 40
        cases = [
            ([(0, "true", ""), (0, sha, ""), RuntimeError("timed out")], None),
            ([(0, "true", ""), (0, sha, ""), (0, "ok", ""), (1, "", "readback failed")], True),
            ([(0, "true", ""), (0, sha, ""), (0, "ok", ""), (0, "b" * 40 + "\trefs/heads/main", "")], True),
        ]
        for effects, expected in cases:
            with self.subTest(expected=expected), patch.object(hg, "_run", side_effect=effects):
                result = hg._git("git_push", "owner/repo", {"cwd": str(Path.cwd()), "branch": "main"})
            self.assertFalse(result["ok"])
            self.assertFalse(result["verified"])
            self.assertFalse(result["retry_safe"])
            self.assertIs(result["write_succeeded"], expected)

    def test_descendant_pipe_timeout_is_bounded(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            hg._run([sys.executable, "-c", code], timeout=0.2)


if __name__ == "__main__":
    unittest.main()
