"""Small regression checks for conservative review and transport isolation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import importlib.util
import sys
import reviewer

_plugin_spec = importlib.util.spec_from_file_location(
    "continuation_review_test_plugin", Path(__file__).with_name("__init__.py"),
    submodule_search_locations=[str(Path(__file__).parent)],
)
assert _plugin_spec is not None and _plugin_spec.loader is not None
plugin = importlib.util.module_from_spec(_plugin_spec)
sys.modules[_plugin_spec.name] = plugin
_plugin_spec.loader.exec_module(plugin)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = {"session_id": "20261025_123456_ab12", "transcript": "Implement the requested feature",
                    "cancelled": False, "awaiting_approval": False, "subagent_running": False,
                    "session_active": False}
        self.response = {"answers": {"decision": {
            "type": "choice", "choice": "continue", "confidence": 0.91,
            "probabilities": {"continue": 0.91, "stop": 0.04, "wait": 0.05},
        }}}

    def test_dry_run_has_no_transport_and_no_resume(self):
        with patch.object(reviewer, "_post_jev", side_effect=AssertionError("network call")):
            out = reviewer.decide(self.case)
        self.assertEqual(out["decision"], "wait")
        self.assertEqual(out["reason"], "dry_run_no_external_review")
        self.assertIsNone(out["resume_command"])
        self.assertFalse(out["resume_executed"])

    def test_injected_transport_and_exact_public_resume_argv(self):
        calls = []
        out = reviewer.decide(self.case, transport=lambda body: calls.append(body) or self.response)
        self.assertEqual(out["decision"], "continue")
        self.assertEqual(out["resume_command"], ["hermes", "--resume", self.case["session_id"]])
        self.assertFalse(out["resume_executed"])
        self.assertEqual(calls[0]["model"], "jev-latest")
        self.assertEqual(calls[0]["questions"]["decision"]["type"], "choice")

    def test_latest_question_stops_but_unfinished_execution_continues(self):
        calls = []
        out = reviewer.decide(self.case, transport=lambda body: calls.append(body) or {
            "answers": {"decision": {"type": "choice", "choice": "stop", "confidence": 0.94,
                         "probabilities": {"continue": 0.03, "stop": 0.94, "wait": 0.03}}}})
        self.assertEqual(out["decision"], "stop")
        instructions = calls[0]["questions"]["decision"]["instructions"]
        self.assertIn("Mere questions", instructions)
        self.assertIn("cancelled or superseded", instructions)
        exec_case = {**self.case, "transcript": "User: Implement feature X. Assistant: Starting implementation; tests remain."}
        continued = reviewer.decide(exec_case, transport=lambda _: self.response)
        self.assertEqual(continued["decision"], "continue")

    def test_cancel_waits_and_running_or_approval_never_call_transport(self):
        never = lambda _: self.fail("transport must not run")
        self.assertEqual(reviewer.decide({**self.case, "cancelled": True}, transport=never)["decision"], "stop")
        for flag in ("awaiting_approval", "subagent_running", "session_active"):
            out = reviewer.decide({**self.case, flag: True}, transport=never)
            self.assertEqual(out["decision"], "wait", flag)
            self.assertIsNone(out["resume_command"])

    def test_low_confidence_and_bad_response_fail_closed(self):
        low = {"answers": {"decision": {"type": "choice", "choice": "continue", "confidence": 0.2,
               "probabilities": {"continue": 0.5, "stop": 0.2, "wait": 0.3}}}}
        self.assertEqual(reviewer.decide(self.case, transport=lambda _: low)["decision"], "wait")
        for response in ({}, {"answers": {"decision": {"type": "choice", "choice": "expand_scope"}}}):
            with self.assertRaises(reviewer.ReviewError):
                reviewer.decide(self.case, transport=lambda _, r=response: r)

    def test_calibrated_live_fixture_allows_clear_jev_continue_at_0_43(self):
        response = {"answers": {"decision": {
            "type": "choice", "choice": "continue", "confidence": 0.43,
            "probabilities": {"continue": 0.62, "stop": 0.11, "wait": 0.27},
        }}}
        out = reviewer.decide(self.case, transport=lambda _: response)
        self.assertEqual(out["decision"], "continue")

    def test_continue_requires_probability_margin_even_above_confidence_floor(self):
        response = {"answers": {"decision": {
            "type": "choice", "choice": "continue", "confidence": 0.60,
            "probabilities": {"continue": 0.55, "stop": 0.15, "wait": 0.30},
        }}}
        self.assertEqual(reviewer.decide(self.case, transport=lambda _: response)["decision"], "wait")

    def test_session_id_and_types_are_validated(self):
        for value in ("latest", "--provider=foo", "../x", "x;true", "x\ny", 4):
            with self.assertRaises(reviewer.ReviewError):
                reviewer.decide({**self.case, "session_id": value})
        with self.assertRaises(reviewer.ReviewError):
            reviewer.decide({key: value for key, value in self.case.items() if key != "session_active"})
        with self.assertRaises(reviewer.ReviewError):
            reviewer.decide({**self.case, "session_active": "false"})

    def test_cli_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_text(json.dumps(self.case), encoding="utf-8")
            with patch.object(reviewer, "_post_jev", side_effect=AssertionError("network call")), patch("sys.stdout") as stdout:
                self.assertEqual(reviewer.main([str(path)]), 0)
                printed = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
            self.assertIn('"decision": "wait"', printed)

    def test_profile_env_key_and_public_session_export(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text('OTHER=value\nTYPESAFE_API_KEY="local-secret"\n', encoding="utf-8")
            with patch.dict("os.environ", {"HERMES_HOME": directory}, clear=True):
                self.assertEqual(reviewer._jev_key(), "local-secret")
        exported = {"id": self.case["session_id"], "messages": [
            {"role": "system", "content": "exclude"}, {"role": "user", "content": "Build X"},
            {"role": "assistant", "content": "Incomplete"}]}
        def fake_cli(argv, **kwargs):
            self.assertEqual(argv[-1], self.case["session_id"])
            return type("Completed", (), {"stdout": json.dumps(exported)})()
        case = reviewer.export_session(self.case["session_id"], runner=fake_cli)
        self.assertEqual(json.loads(case["transcript"]), [
            {"role": "user", "content": "Build X"}, {"role": "assistant", "content": "Incomplete"}])
        self.assertNotIn("system", case["transcript"])

    def test_registered_post_turn_hook_judges_then_injects_in_live_hook_context(self):
        hooks = {}
        class Context:
            def register_hook(self, event, callback):
                hooks[event] = callback
            def inject_message(self, content, role="user", **kwargs):
                injected.append((content, role, kwargs))
                return True
        injected = []
        plugin.register(Context())
        self.assertIn("post_llm_call", hooks)
        history = [
            {"role": "user", "content": "Implement CSV export and run tests."},
            {"role": "assistant", "content": "I implemented it; tests remain."},
        ]
        with patch.object(plugin, "decide", return_value={"decision": "continue", "reason": "jev_choice", "confidence": 0.91}):
            hooks["post_llm_call"](
                session_id="ses_disposable_001", user_message="Implement CSV export",
                assistant_response="I implemented it; tests remain.",
                conversation_history=history, model="test", platform="cli",
            )
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0][1], "user")
        self.assertIn("already-authorized task", injected[0][0])
        self.assertEqual(injected[0][2], {})

    def test_registered_hook_does_not_inject_for_answered_question_or_wait(self):
        hooks = {}
        injected = []
        class Context:
            def register_hook(self, event, callback):
                hooks[event] = callback
            def inject_message(self, *args, **kwargs):
                injected.append(args)
                return True
        plugin.register(Context())
        for choice, confidence, probabilities in (
            ("stop", 0.94, {"continue": 0.03, "stop": 0.94, "wait": 0.03}),
            ("wait", 0.92, {"continue": 0.01, "stop": 0.04, "wait": 0.95}),
        ):
            response = {"answers": {"decision": {"type": "choice", "choice": choice,
                        "confidence": confidence, "probabilities": probabilities}}}
            with patch.object(plugin, "decide", return_value={"decision": choice, "reason": "jev_choice", "confidence": confidence}):
                hooks["post_llm_call"](
                    session_id="ses_disposable_002", user_message="What is 2+2?",
                    assistant_response="The deployment needs approval before action.",
                    conversation_history=[{"role": "user", "content": "What is 2+2?"},
                                          {"role": "assistant", "content": "4; deployment waits for approval."}],
                    model="test", platform="cli",
                )
        self.assertEqual(injected, [])


if __name__ == "__main__":
    unittest.main()
